from __future__ import annotations

import asyncio
import html
import logging
import secrets
import shutil
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest, TelegramNetworkError, TelegramRetryAfter
from aiogram.enums import ChatAction
from aiogram.types import FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup, Message

from app.config import Settings
from app.database import CachedFile, Database
from app.downloader import (
    DownloadCancelled,
    DownloadedMedia,
    FileTooLargeError,
    MediaDownloadError,
    ProgressUpdate,
    download_media,
    human_size,
)

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class ActiveJob:
    user_id: int
    cancel_event: asyncio.Event


@dataclass(slots=True)
class QueueState:
    waiting: int = 0


async def safe_edit(message: Message, text: str, reply_markup: InlineKeyboardMarkup | None = None) -> None:
    try:
        await message.edit_text(text, reply_markup=reply_markup)
    except TelegramAPIError:
        pass


def cancel_keyboard(job_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="❌ لغو عملیات", callback_data=f"j:{job_id}:cancel")]]
    )


async def process_url(
    message: Message,
    bot: Bot,
    settings: Settings,
    database: Database,
    download_semaphore: asyncio.Semaphore,
    user_locks: dict[int, asyncio.Lock],
    active_jobs: dict[str, ActiveJob],
    queue_state: QueueState,
    url: str,
    status: Message | None = None,
    format_id: str | None = None,
    audio_bitrate: int | None = None,
    subtitles: bool = False,
    quality_label: str | None = None,
    job_user_id: int | None = None,
    force_direct: bool = False,
    manage_lock: bool = True,
) -> None:
    user_id = job_user_id or (message.from_user.id if message.from_user else message.chat.id)
    if manage_lock:
        lock = user_locks.setdefault(user_id, asyncio.Lock())
        if lock.locked():
            await message.answer("⏳ یک عملیات دیگر برای شما در حال انجام است؛ ابتدا تمام یا لغو شود.")
            return
        async with lock:
            await _process_url_inner(
                message, bot, settings, database, download_semaphore, active_jobs,
                queue_state, url, user_id, status, format_id, audio_bitrate,
                subtitles, quality_label, force_direct,
            )
    else:
        await _process_url_inner(
            message, bot, settings, database, download_semaphore, active_jobs,
            queue_state, url, user_id, status, format_id, audio_bitrate,
            subtitles, quality_label, force_direct,
        )


async def _process_url_inner(
    message: Message,
    bot: Bot,
    settings: Settings,
    database: Database,
    download_semaphore: asyncio.Semaphore,
    active_jobs: dict[str, ActiveJob],
    queue_state: QueueState,
    url: str,
    user_id: int,
    status: Message | None,
    format_id: str | None,
    audio_bitrate: int | None,
    subtitles: bool,
    quality_label: str | None,
    force_direct: bool,
) -> None:
    if user_id not in settings.admin_ids:
        allowed, reason, *_ = await database.quota_status(
            user_id, settings.daily_download_limit, settings.daily_size_limit
        )
        if not allowed:
            await message.answer(f"⛔ {reason}")
            return

    job_id = secrets.token_hex(8)
    cancel_event = asyncio.Event()
    active_jobs[job_id] = ActiveJob(user_id, cancel_event)
    if status is None:
        status = await message.reply("🔎 درخواست ثبت شد…")
    await database.create_job(job_id, user_id, url, quality_label or "auto")
    outcome = "error"
    outcome_size: int | None = None
    outcome_error: str | None = None
    loop = asyncio.get_running_loop()
    progress_closed = False
    last_progress = 0.0

    async def edit_progress(text: str) -> None:
        if not progress_closed:
            await safe_edit(status, text, reply_markup=cancel_keyboard(job_id))

    def on_progress(update: ProgressUpdate) -> None:
        nonlocal last_progress
        now = time.monotonic()
        complete = update.total is not None and update.downloaded >= update.total
        if now - last_progress < 4 and not complete:
            return
        last_progress = now
        text = _progress_text(update, quality_label)

        def schedule() -> None:
            if not progress_closed:
                asyncio.create_task(edit_progress(text))

        loop.call_soon_threadsafe(schedule)

    try:
        mode = "audio" if audio_bitrate else "video"
        format_key = format_id or "auto"
        cache_key = database.cache_key(url, f"{format_key}:{audio_bitrate or 0}", mode, subtitles)
        cached = await database.get_cached_file(cache_key)
        if cached:
            if user_id not in settings.admin_ids and not await database.ensure_daily_capacity(
                user_id, cached.size, settings.daily_size_limit
            ):
                await safe_edit(status, "⛔ حجم این فایل از سهمیه‌ی باقی‌مانده‌ی امروز شما بیشتر است.")
                outcome_error = "daily byte quota"
                return
            try:
                sent = await _send_cached(bot, message.chat.id, url, cached)
                await _announce_success(message, sent, cached.size)
                await database.record_download(user_id, cached.size)
                outcome, outcome_size = "done", cached.size
                messenger = "بله" if settings.platform == "bale" else "تلگرام"
                await safe_edit(status, f"⚡ از کش {messenger} ارسال شد؛ بدون دانلود مجدد.")
                return
            except TelegramBadRequest:
                await database.delete_cached_file(cache_key)

        queued = False
        if download_semaphore.locked():
            queue_state.waiting += 1
            queued = True
            await safe_edit(
                status,
                f"⌛ درخواست در صف است؛ نوبت تقریبی: {queue_state.waiting}",
                reply_markup=cancel_keyboard(job_id),
            )
        try:
            acquired = await _acquire_semaphore(download_semaphore, cancel_event)
            if not acquired:
                raise DownloadCancelled("عملیات در صف لغو شد.")
        finally:
            if queued:
                queue_state.waiting = max(0, queue_state.waiting - 1)

        try:
            with tempfile.TemporaryDirectory(prefix="job-", dir=settings.download_dir) as temp_dir:
                selected = f" با کیفیت {quality_label}" if quality_label else ""
                await safe_edit(
                    status, f"⬇️ در حال دانلود{selected}…", reply_markup=cancel_keyboard(job_id)
                )
                media = await download_media(
                    url=url,
                    directory=Path(temp_dir),
                    max_size=settings.max_file_size,
                    timeout=settings.request_timeout,
                    ytdlp_max_height=settings.ytdlp_max_height,
                    cookies_file=settings.cookies_file,
                    proxy=settings.ytdlp_proxy,
                    format_id=format_id,
                    audio_bitrate=audio_bitrate,
                    subtitles=subtitles,
                    auto_compress=settings.auto_compress,
                    allow_oversize=bool(settings.public_base_url),
                    progress_callback=on_progress,
                    cancel_event=cancel_event,
                    force_direct=force_direct,
                )
                _raise_if_cancelled(cancel_event)
                outcome_size = media.size
                if user_id not in settings.admin_ids and not await database.ensure_daily_capacity(
                    user_id, media.size, settings.daily_size_limit
                ):
                    raise MediaDownloadError("حجم این فایل از سهمیه‌ی باقی‌مانده‌ی امروز شما بیشتر است.")

                if media.size > settings.max_file_size:
                    if not settings.public_base_url:
                        raise FileTooLargeError(
                            f"حجم نهایی ({human_size(media.size)}) از سقف آپلود پیام‌رسان بیشتر است؛ کیفیت پایین‌تری انتخاب کنید."
                        )
                    link = await _share_media(media, settings, database)
                    await message.answer(
                        f"✅ <b>فایل آماده است.</b>\n📦 حجم: {human_size(media.size)}\n\n"
                        f"🔗 <a href=\"{link}\">دانلود فایل</a>\n"
                        f"⌛ این لینک تا {settings.file_link_ttl // 60} دقیقه معتبر است."
                    )
                    await database.record_download(user_id, media.size)
                    outcome = "done"
                    await safe_edit(status, "✅ لینک موقت ساخته شد و فایل در زمان مقرر پاک می‌شود.")
                    return

                await safe_edit(
                    status,
                    f"⬆️ دانلود تمام شد ({human_size(media.size)})؛ در حال آپلود…",
                    reply_markup=cancel_keyboard(job_id),
                )
                action = ChatAction.UPLOAD_AUDIO if media.is_audio else ChatAction.UPLOAD_VIDEO if media.is_video else ChatAction.UPLOAD_DOCUMENT
                # Bale's API currently replies "action not found" for upload actions.
                if settings.platform != "bale":
                    await bot.send_chat_action(message.chat.id, action)
                sent = await _upload_media(bot, message.chat.id, url, media)
                _raise_if_cancelled(cancel_event)
                file_id, unique_id, file_type = _telegram_attachment(sent)
                await database.save_cached_file(
                    cache_key, url, format_key, file_id, unique_id, file_type, media.title, media.size
                )
                await database.record_download(user_id, media.size)
                await _announce_success(message, sent, media.size)
                outcome = "done"
                await safe_edit(status, "✅ کار تمام شد؛ فایل موقت پاک شد و file_id برای دفعات بعد ذخیره شد.")
        finally:
            download_semaphore.release()

    except DownloadCancelled as exc:
        outcome, outcome_error = "cancelled", str(exc)
        await safe_edit(status, "❌ عملیات لغو و فایل موقت پاک شد.")
    except asyncio.TimeoutError:
        outcome_error = "timeout"
        await safe_edit(status, "⌛ زمان دانلود بیش از حد مجاز شد.")
    except MediaDownloadError as exc:
        outcome_error = str(exc)
        await safe_edit(status, f"❌ {html.escape(str(exc))}")
    except TelegramRetryAfter as exc:
        outcome_error = "rate limited"
        await safe_edit(status, f"⏳ محدودیت پیام‌رسان؛ {exc.retry_after} ثانیه بعد تلاش کنید.")
    except TelegramNetworkError:
        outcome_error = "messenger network"
        logger.exception("Messenger network error")
        await safe_edit(status, "❌ ارتباط با پیام‌رسان قطع شد.")
    except TelegramBadRequest as exc:
        outcome_error = str(exc)
        logger.warning("Messenger rejected upload: %s", exc)
        await safe_edit(status, "❌ پیام‌رسان فایل را قبول نکرد؛ حجم یا قالب مجاز نیست.")
    except Exception as exc:
        outcome_error = str(exc)
        logger.exception("Unexpected processing error")
        await safe_edit(status, "❌ خطای غیرمنتظره رخ داد؛ جزئیات در لاگ ثبت شد.")
    finally:
        progress_closed = True
        active_jobs.pop(job_id, None)
        await database.finish_job(job_id, outcome, outcome_size, outcome_error)


async def _acquire_semaphore(semaphore: asyncio.Semaphore, cancel_event: asyncio.Event) -> bool:
    acquire_task = asyncio.create_task(semaphore.acquire())
    cancel_task = asyncio.create_task(cancel_event.wait())
    done, _ = await asyncio.wait({acquire_task, cancel_task}, return_when=asyncio.FIRST_COMPLETED)
    if cancel_task in done and cancel_task.result():
        if acquire_task.done() and not acquire_task.cancelled():
            if acquire_task.result():
                semaphore.release()
        else:
            acquire_task.cancel()
        return False
    cancel_task.cancel()
    return True


def _raise_if_cancelled(event: asyncio.Event) -> None:
    if event.is_set():
        raise DownloadCancelled("عملیات توسط کاربر لغو شد.")


def _progress_text(update: ProgressUpdate, quality: str | None) -> str:
    names = {"download": "⬇️ دانلود", "processing": "⚙️ پردازش", "compress": "🗜 فشرده‌سازی"}
    title = names.get(update.stage, "⏳ پردازش")
    lines = [f"{title} {html.escape(quality or '')}".strip()]
    if update.total:
        percent = min(100, update.downloaded * 100 / update.total)
        filled = min(10, int(percent / 10))
        lines.append(f"{'▓' * filled}{'░' * (10 - filled)} {percent:.0f}%")
        if update.stage == "compress":
            lines.append("در حال تنظیم حجم خروجی…")
        else:
            lines.append(f"{human_size(update.downloaded)} از {human_size(update.total)}")
    else:
        lines.append(human_size(update.downloaded))
    if update.speed:
        lines.append(f"سرعت: {human_size(int(update.speed))}/ثانیه")
    if update.eta:
        lines.append(f"زمان تقریبی: {update.eta} ثانیه")
    return "\n".join(lines)


async def _share_media(media: DownloadedMedia, settings: Settings, database: Database) -> str:
    token = secrets.token_urlsafe(24)
    directory = settings.file_storage_dir / token
    directory.mkdir(parents=True, exist_ok=False)
    destination = directory / media.filename
    await asyncio.to_thread(shutil.move, str(media.path), str(destination))
    expires_at = int(time.time()) + settings.file_link_ttl
    await database.save_shared_file(token, destination, media.filename, media.size, expires_at)
    return f"{settings.public_base_url}/files/{token}"


async def cleanup_shared_files(settings: Settings, database: Database) -> None:
    while True:
        try:
            for shared in await database.expired_shared_files():
                try:
                    await asyncio.to_thread(shutil.rmtree, str(shared.path.parent), True)
                finally:
                    await database.delete_shared_file(shared.token)
        except Exception:
            logger.exception("Shared-file cleanup failed")
        await asyncio.sleep(900)


async def _upload_media(bot: Bot, chat_id: int, url: str, media: DownloadedMedia) -> Message:
    caption = _caption(media.title, media.size, url)
    input_file = FSInputFile(media.path, filename=media.filename)
    thumbnail = FSInputFile(media.thumbnail) if media.thumbnail and media.thumbnail.is_file() else None
    if media.is_audio:
        return await bot.send_audio(
            chat_id, input_file, caption=caption, title=media.title[:64], thumbnail=thumbnail
        )
    if media.is_video:
        try:
            return await bot.send_video(
                chat_id, input_file, caption=caption, supports_streaming=True,
                duration=media.duration, thumbnail=thumbnail,
            )
        except TelegramBadRequest as exc:
            logger.info("Sending video as document instead: %s", exc)
            input_file = FSInputFile(media.path, filename=media.filename)
    return await bot.send_document(chat_id, input_file, caption=caption, thumbnail=thumbnail)


async def _send_cached(bot: Bot, chat_id: int, url: str, cached: CachedFile) -> Message:
    caption = _caption(cached.title, cached.size, url)
    if cached.file_type == "video":
        return await bot.send_video(chat_id, cached.file_id, caption=caption, supports_streaming=True)
    if cached.file_type == "audio":
        return await bot.send_audio(chat_id, cached.file_id, caption=caption)
    return await bot.send_document(chat_id, cached.file_id, caption=caption)


def _caption(title: str, size: int, url: str) -> str:
    safe_title = html.escape(title[:600])
    source = html.escape(urlsplit(url).hostname or "لینک ارسالی")
    return (
        f"🎬 <b>{safe_title}</b>\n📦 {human_size(size)}\n🔗 {source}\n\n"
        "💎 <a href=\"https://sirgsm.ir\">sirgsm.ir</a>"
    )


async def _announce_success(message: Message, sent: Message, size: int) -> None:
    file_id, unique_id, _ = _telegram_attachment(sent)
    await message.answer(
        "✅ <b>عملیات انجام شد.</b>\n"
        f"📦 حجم: {human_size(size)}\n"
        f"🆔 <code>{html.escape(file_id)}</code>\n"
        f"🔑 unique_id: <code>{html.escape(unique_id)}</code>"
    )


def _telegram_attachment(message: Message) -> tuple[str, str, str]:
    for file_type, attachment in (
        ("video", message.video), ("audio", message.audio),
        ("document", message.document), ("animation", message.animation),
    ):
        if attachment:
            return attachment.file_id, attachment.file_unique_id, file_type
    return "نامشخص", "نامشخص", "document"
