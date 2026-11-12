from __future__ import annotations

import asyncio
import html
import logging
import secrets
import time
from dataclasses import dataclass, field

import uvicorn
from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiogram.enums import ChatMemberStatus, ParseMode
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    BotCommand, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message,
)

from app.admin import router as admin_router
from app.config import Settings
from app.database import Database
from app.downloader import MediaDownloadError, extract_url, human_size, should_probe
from app.processor import (
    ActiveJob, QueueState, cleanup_shared_files, process_url, safe_edit,
)
from app.quality import (
    QualityChoice, probe_playlist, probe_video,
)

logger = logging.getLogger(__name__)
dp = Dispatcher()
dp.include_router(admin_router)

SPONSOR_URL = "https://sirgsm.ir"
DEVELOPER_URL = "https://t.me/nhaghbayan"
QUALITY_SESSION_TTL = 15 * 60
MAX_QUALITY_SESSIONS = 100
BRANDING_HTML = (
    "💎 <b>اسپانسر:</b> <a href=\"https://sirgsm.ir\">sirgsm.ir</a>\n"
    "👨‍💻 <b>سازنده:</b> Navid Haghbayan\n"
    "💬 <b>تلگرام سازنده:</b> @nhaghbayan"
)
BOT_DESCRIPTION = (
    "دانلود از YouTube، Instagram، TikTok، Aparat و سایت‌های بیشتر با انتخاب کیفیت، "
    "MP3، زیرنویس و لینک موقت.\n\n💎 اسپانسر: https://sirgsm.ir\n"
    "👨‍💻 سازنده: Navid Haghbayan\n💬 https://t.me/nhaghbayan"
)
BOT_SHORT_DESCRIPTION = "دانلود فایل و ویدیو با انتخاب کیفیت و MP3 | اسپانسر sirgsm.ir"
HELP_TEXT = (
    "<b>ربات دانلود و آپلود لینک</b>\n\n"
    "• لینک مستقیم فایل\n• YouTube و Instagram\n• TikTok، Aparat، X، SoundCloud و سایت‌های بیشتر\n\n"
    "کیفیت و حجم تقریبی را انتخاب کنید یا خروجی MP3 بگیرید. دانلود تکراری از کش پیام‌رسان، "
    "سریع و بدون مصرف مجدد ترافیک سرور ارسال می‌شود.\n\n"
    "دستورها:\n/quota مشاهده سهمیه\n/about درباره ما\n/limits محدودیت‌ها\n\n"
    + BRANDING_HTML
)
ABOUT_TEXT = (
    "<b>ℹ️ درباره ما</b>\n\n"
    "این ربات برای تبدیل لینک فایل و ویدیو به فایل قابل دانلود در تلگرام و بله ساخته شده است.\n\n"
    "💎 <b>اسپانسر و حامی پروژه:</b>\n<a href=\"https://sirgsm.ir\">sirgsm.ir</a>\n\n"
    "👨‍💻 <b>سازنده و توسعه‌دهنده:</b>\nNavid Haghbayan\n\n"
    "💬 <b>ارتباط مستقیم با سازنده:</b>\n@nhaghbayan\n\nاز همراهی شما سپاسگزاریم. 🙏"
)


@dataclass(slots=True)
class PendingQuality:
    url: str
    user_id: int
    title: str
    choices: list[QualityChoice]
    created_at: float
    duration: int | None = None
    entries: list = field(default_factory=list)
    subtitles_available: bool = False
    subtitles: bool = False


def about_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🌐 وب‌سایت اسپانسر", url=SPONSOR_URL)],
        [InlineKeyboardButton(text="💬 ارتباط با سازنده", url=DEVELOPER_URL)],
    ])


def main_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="📊 سهمیه من", callback_data="menu:quota"),
            InlineKeyboardButton(text="⚙️ محدودیت‌ها", callback_data="menu:limits"),
        ],
        [
            InlineKeyboardButton(text="📚 راهنما", callback_data="menu:help"),
            InlineKeyboardButton(text="ℹ️ درباره ما", callback_data="menu:about"),
        ],
        [InlineKeyboardButton(text="🌐 وب‌سایت اسپانسر", url=SPONSOR_URL)],
        [InlineKeyboardButton(text="💬 ارتباط با سازنده", url=DEVELOPER_URL)],
    ])


async def check_access(message: Message, bot: Bot, settings: Settings, database: Database) -> bool:
    if not message.from_user:
        return False
    user = message.from_user
    await database.touch_user(user.id, user.username, user.first_name)
    if user.id in settings.admin_ids:
        return True
    if settings.allowed_user_ids and user.id not in settings.allowed_user_ids:
        await message.answer("⛔ دسترسی شما به این ربات محدود شده است.")
        return False
    row = await database.get_user(user.id)
    if row and row["blocked"]:
        await message.answer("⛔ دسترسی شما توسط مدیر مسدود شده است.")
        return False
    if settings.force_join_channel:
        try:
            member = await bot.get_chat_member(settings.force_join_channel, user.id)
            accepted = {
                ChatMemberStatus.MEMBER, ChatMemberStatus.ADMINISTRATOR,
                ChatMemberStatus.CREATOR, ChatMemberStatus.RESTRICTED,
            }
            if member.status not in accepted:
                raise ValueError("not member")
        except (TelegramAPIError, ValueError):
            keyboard = None
            if settings.force_join_channel.startswith("@"):
                keyboard = InlineKeyboardMarkup(inline_keyboard=[[
                    InlineKeyboardButton(
                        text="📢 عضویت در کانال",
                        url=f"https://t.me/{settings.force_join_channel[1:]}",
                    )
                ]])
            await message.answer(
                "برای استفاده از ربات ابتدا در کانال ما عضو شوید و دوباره تلاش کنید.",
                reply_markup=keyboard,
            )
            return False
    return True


@dp.message(CommandStart())
async def start_handler(message: Message, bot: Bot, settings: Settings, database: Database) -> None:
    if await check_access(message, bot, settings, database):
        await message.answer(HELP_TEXT, reply_markup=main_menu_keyboard())


@dp.message(Command("about"))
async def about_handler(message: Message, bot: Bot, settings: Settings, database: Database) -> None:
    if await check_access(message, bot, settings, database):
        await message.answer(ABOUT_TEXT, reply_markup=about_keyboard())


@dp.message(Command("help"))
async def help_handler(message: Message, bot: Bot, settings: Settings, database: Database) -> None:
    await start_handler(message, bot, settings, database)


@dp.message(Command("limits"))
async def limits_handler(message: Message, bot: Bot, settings: Settings, database: Database) -> None:
    if not await check_access(message, bot, settings, database):
        return
    await message.answer(
        f"⚙️ سقف آپلود: <b>{human_size(settings.max_file_size)}</b>\n"
        f"🎬 حداکثر کیفیت: <b>{settings.ytdlp_max_height}p</b>\n"
        f"🔄 کارهای هم‌زمان: <b>{settings.max_concurrent_jobs}</b>\n"
        f"🗜 فشرده‌سازی: <b>{'روشن' if settings.auto_compress else 'خاموش'}</b>"
    )


@dp.message(Command("quota"))
async def quota_handler(message: Message, bot: Bot, settings: Settings, database: Database) -> None:
    if not await check_access(message, bot, settings, database):
        return
    user_id = message.from_user.id
    _, _, count_limit, byte_limit, remaining_count, remaining_bytes = await database.quota_status(
        user_id, settings.daily_download_limit, settings.daily_size_limit
    )
    await message.answer(
        "📊 <b>سهمیه‌ی امروز شما</b>\n"
        f"📥 باقی‌مانده: <b>{remaining_count}</b> از {count_limit} دانلود\n"
        f"📦 حجم باقی‌مانده: <b>{human_size(remaining_bytes)}</b> از {human_size(byte_limit)}\n"
        "🕛 سهمیه هر روز به وقت تهران تازه می‌شود."
    )


@dp.callback_query(F.data.startswith("menu:"))
async def main_menu_callback_handler(
    callback: CallbackQuery,
    settings: Settings,
    database: Database,
) -> None:
    if not isinstance(callback.message, Message):
        await callback.answer("این پیام دیگر قابل استفاده نیست.", show_alert=True)
        return
    user = callback.from_user
    await database.touch_user(user.id, user.username, user.first_name)
    row = await database.get_user(user.id)
    if row and row["blocked"] and user.id not in settings.admin_ids:
        await callback.answer("دسترسی شما مسدود شده است.", show_alert=True)
        return
    if settings.allowed_user_ids and user.id not in settings.allowed_user_ids and user.id not in settings.admin_ids:
        await callback.answer("دسترسی شما محدود شده است.", show_alert=True)
        return

    action = (callback.data or "").split(":", 1)[1]
    if action == "quota":
        if user.id in settings.admin_ids:
            text = "📊 <b>سهمیه‌ی ادمین</b>\n♾️ دانلود و حجم روزانه برای شما نامحدود است."
        else:
            _, _, count_limit, byte_limit, remaining_count, remaining_bytes = await database.quota_status(
                user.id, settings.daily_download_limit, settings.daily_size_limit
            )
            text = (
                "📊 <b>سهمیه‌ی امروز شما</b>\n"
                f"📥 باقی‌مانده: <b>{remaining_count}</b> از {count_limit} دانلود\n"
                f"📦 حجم باقی‌مانده: <b>{human_size(remaining_bytes)}</b> از {human_size(byte_limit)}\n"
                "🕛 سهمیه هر روز به وقت تهران تازه می‌شود."
            )
        await callback.message.answer(text)
    elif action == "limits":
        await callback.message.answer(
            f"⚙️ سقف آپلود: <b>{human_size(settings.max_file_size)}</b>\n"
            f"🎬 حداکثر کیفیت: <b>{settings.ytdlp_max_height}p</b>\n"
            f"🔄 کارهای هم‌زمان: <b>{settings.max_concurrent_jobs}</b>\n"
            f"🗜 فشرده‌سازی: <b>{'روشن' if settings.auto_compress else 'خاموش'}</b>"
        )
    elif action == "about":
        await callback.message.answer(ABOUT_TEXT, reply_markup=about_keyboard())
    elif action == "help":
        await callback.message.answer(HELP_TEXT, reply_markup=main_menu_keyboard())
    else:
        await callback.answer("گزینه نامعتبر است.", show_alert=True)
        return
    await callback.answer()


@dp.message(F.text)
async def link_handler(
    message: Message,
    bot: Bot,
    settings: Settings,
    database: Database,
    download_semaphore: asyncio.Semaphore,
    user_locks: dict[int, asyncio.Lock],
    quality_sessions: dict[str, PendingQuality],
    active_jobs: dict[str, ActiveJob],
    queue_state: QueueState,
) -> None:
    if not await check_access(message, bot, settings, database):
        return
    url = extract_url(message.text or "")
    if not url:
        await message.answer("لینک کاملی که با http یا https شروع شود بفرستید.")
        return
    user_id = message.from_user.id
    if user_id not in settings.admin_ids:
        allowed, reason, *_ = await database.quota_status(
            user_id, settings.daily_download_limit, settings.daily_size_limit
        )
        if not allowed:
            await message.answer(f"⛔ {reason}")
            return
    dependencies = (
        message, bot, settings, database, download_semaphore, user_locks,
        active_jobs, queue_state, url,
    )
    if should_probe(url, settings.ytdlp_all_sites):
        await _show_quality_menu(*dependencies, quality_sessions)
    else:
        await process_url(*dependencies)


async def _show_quality_menu(
    message: Message,
    bot: Bot,
    settings: Settings,
    database: Database,
    download_semaphore: asyncio.Semaphore,
    user_locks: dict[int, asyncio.Lock],
    active_jobs: dict[str, ActiveJob],
    queue_state: QueueState,
    url: str,
    quality_sessions: dict[str, PendingQuality],
) -> None:
    user_id = message.from_user.id
    lock = user_locks.setdefault(user_id, asyncio.Lock())
    if lock.locked():
        await message.answer("⏳ درخواست قبلی شما هنوز تمام نشده است.")
        return
    async with lock:
        status = await message.reply("🔎 در حال دریافت کیفیت‌ها و حجم تقریبی…")
        try:
            details = await asyncio.wait_for(
                asyncio.to_thread(
                    probe_video, url, settings.ytdlp_max_height,
                    settings.cookies_file, settings.ytdlp_proxy,
                ),
                timeout=90,
            )
            token = _store_quality_session(
                quality_sessions,
                PendingQuality(
                    url, user_id, details.title, details.choices, time.monotonic(),
                    duration=details.duration, subtitles_available=details.subtitles_available,
                ),
            )
            await safe_edit(
                status, _quality_menu_text(details.title, details.duration, settings),
                reply_markup=_quality_keyboard(token, details.choices, settings),
            )
        except MediaDownloadError as exc:
            if str(exc) == "UNSUPPORTED_URL":
                await process_url(
                    message, bot, settings, database, download_semaphore, user_locks,
                    active_jobs, queue_state, url, status=status, force_direct=True,
                    manage_lock=False,
                )
            elif str(exc) == "PLAYLIST_DETECTED":
                await _show_playlist_menu(
                    message, settings, url, status, quality_sessions, user_id,
                )
            else:
                await safe_edit(status, f"❌ {html.escape(str(exc))}")
        except asyncio.TimeoutError:
            await safe_edit(status, "⌛ دریافت کیفیت‌ها بیش از حد طول کشید.")
        except Exception:
            logger.exception("Unexpected format probing error")
            await safe_edit(status, "❌ دریافت کیفیت‌ها با خطا متوقف شد.")


async def _show_playlist_menu(
    message: Message,
    settings: Settings,
    url: str,
    status: Message,
    sessions: dict[str, PendingQuality],
    user_id: int,
) -> None:
    try:
        details = await asyncio.wait_for(
            asyncio.to_thread(
                probe_playlist, url, settings.max_playlist_items,
                settings.cookies_file, settings.ytdlp_proxy,
            ),
            timeout=90,
        )
        choices = [
            QualityChoice(480, "best[height<=480]/bestvideo[height<=480]+bestaudio/best", None, "480p"),
            QualityChoice(720, "best[height<=720]/bestvideo[height<=720]+bestaudio/best", None, "720p"),
            QualityChoice(0, "bestaudio/best", None, "MP3 128kbps", kind="audio", audio_bitrate=128),
        ]
        token = _store_quality_session(
            sessions,
            PendingQuality(url, user_id, details.title, choices, time.monotonic(), entries=details.entries),
        )
        note = "\n⚠️ فقط موارد اول دانلود می‌شوند." if details.truncated else ""
        await safe_edit(
            status,
            f"📚 <b>{html.escape(details.title)}</b>\n{len(details.entries)} رسانه آماده‌ی دانلود است.{note}\n"
            "کیفیت موردنظر برای همه را انتخاب کنید:",
            reply_markup=_quality_keyboard(token, choices, settings),
        )
    except (MediaDownloadError, asyncio.TimeoutError) as exc:
        await safe_edit(status, f"❌ {html.escape(str(exc))}")


def _quality_menu_text(title: str, duration: int | None, settings: Settings) -> str:
    duration_text = f"⏱ مدت: {_format_duration(duration)}\n" if duration else ""
    return (
        f"🎬 <b>{html.escape(title[:600])}</b>\n{duration_text}\n"
        "کیفیت یا MP3 را انتخاب کنید؛ حجم‌ها تقریبی هستند:\n"
        f"✅ سقف آپلود: {human_size(settings.max_file_size)}\n"
        "🗜 امکان فشرده‌سازی | 🔗 لینک موقت | 🚫 بزرگ‌تر از سقف"
    )


def _store_quality_session(sessions: dict[str, PendingQuality], pending: PendingQuality) -> str:
    now = time.monotonic()
    for key, value in list(sessions.items()):
        if now - value.created_at > QUALITY_SESSION_TTL:
            sessions.pop(key, None)
    while len(sessions) >= MAX_QUALITY_SESSIONS:
        sessions.pop(next(iter(sessions)))
    token = secrets.token_urlsafe(9)
    while token in sessions:
        token = secrets.token_urlsafe(9)
    sessions[token] = pending
    return token


def _quality_keyboard(
    token: str,
    choices: list[QualityChoice],
    settings_or_max,
    subtitles_available: bool = False,
    subtitles_enabled: bool = False,
) -> InlineKeyboardMarkup:
    if isinstance(settings_or_max, Settings):
        settings = settings_or_max
        max_size = settings.max_file_size
        allow_links = bool(settings.public_base_url)
        auto_compress = settings.auto_compress
    else:
        max_size = int(settings_or_max)
        allow_links = False
        auto_compress = False
    buttons = []
    for index, choice in enumerate(choices):
        size_text = human_size(choice.estimated_size) if choice.estimated_size else "حجم نامشخص"
        over = choice.estimated_size is not None and choice.estimated_size > max_size
        icon = "🎵" if choice.kind == "audio" else "🎬"
        if over:
            icon = "🔗" if allow_links else "🗜" if auto_compress and choice.kind == "video" else "🚫"
        buttons.append(InlineKeyboardButton(
            text=f"{icon} {choice.label} • {size_text}", callback_data=f"q:{token}:c:{index}"
        ))
    rows = [buttons[index:index + 2] for index in range(0, len(buttons), 2)]
    if subtitles_available:
        rows.append([InlineKeyboardButton(
            text=f"💬 زیرنویس: {'روشن ✅' if subtitles_enabled else 'خاموش'}",
            callback_data=f"q:{token}:s",
        )])
    rows.append([InlineKeyboardButton(text="❌ انصراف", callback_data=f"q:{token}:cancel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@dp.callback_query(F.data.startswith("q:"))
async def quality_callback_handler(
    callback: CallbackQuery,
    bot: Bot,
    settings: Settings,
    database: Database,
    download_semaphore: asyncio.Semaphore,
    user_locks: dict[int, asyncio.Lock],
    quality_sessions: dict[str, PendingQuality],
    active_jobs: dict[str, ActiveJob],
    queue_state: QueueState,
) -> None:
    if not isinstance(callback.message, Message):
        await callback.answer("این پیام قابل استفاده نیست.", show_alert=True)
        return
    parts = (callback.data or "").split(":")
    if len(parts) < 3:
        await callback.answer("درخواست نامعتبر.", show_alert=True)
        return
    _, token, action = parts[:3]
    pending = quality_sessions.get(token)
    if not pending or time.monotonic() - pending.created_at > QUALITY_SESSION_TTL:
        quality_sessions.pop(token, None)
        await callback.answer("منو منقضی شده؛ لینک را دوباره بفرستید.", show_alert=True)
        return
    if callback.from_user.id != pending.user_id:
        await callback.answer("این دکمه فقط برای فرستنده‌ی لینک است.", show_alert=True)
        return
    if action == "cancel":
        quality_sessions.pop(token, None)
        await callback.answer("لغو شد")
        await safe_edit(callback.message, "❌ انتخاب لغو شد.")
        return
    if action == "s":
        pending.subtitles = not pending.subtitles
        await callback.message.edit_reply_markup(
            reply_markup=_quality_keyboard(
                token, pending.choices, settings, pending.subtitles_available, pending.subtitles
            )
        )
        await callback.answer(f"زیرنویس {'روشن' if pending.subtitles else 'خاموش'} شد")
        return
    if action != "c" or len(parts) != 4:
        await callback.answer("درخواست نامعتبر.", show_alert=True)
        return
    try:
        choice = pending.choices[int(parts[3])]
    except (ValueError, IndexError):
        await callback.answer("کیفیت نامعتبر است.", show_alert=True)
        return
    over = choice.estimated_size is not None and choice.estimated_size > settings.max_file_size
    can_compress = settings.auto_compress and choice.kind == "video"
    if over and not settings.public_base_url and not can_compress:
        await callback.answer("این کیفیت از سقف بیشتر است؛ گزینه‌ی پایین‌تر را انتخاب کنید.", show_alert=True)
        return
    lock = user_locks.setdefault(pending.user_id, asyncio.Lock())
    if lock.locked():
        await callback.answer("یک عملیات دیگر در حال انجام است.", show_alert=True)
        return
    quality_sessions.pop(token, None)
    await callback.answer(f"{choice.label} انتخاب شد")
    if pending.entries:
        await _process_playlist(
            callback.message, bot, settings, database, download_semaphore,
            user_locks, active_jobs, queue_state, pending, choice,
        )
    else:
        await process_url(
            callback.message, bot, settings, database, download_semaphore,
            user_locks, active_jobs, queue_state, pending.url,
            status=callback.message, format_id=choice.format_id,
            audio_bitrate=choice.audio_bitrate, subtitles=pending.subtitles,
            quality_label=choice.label, job_user_id=pending.user_id,
        )


async def _process_playlist(
    message: Message,
    bot: Bot,
    settings: Settings,
    database: Database,
    semaphore: asyncio.Semaphore,
    user_locks: dict[int, asyncio.Lock],
    active_jobs: dict[str, ActiveJob],
    queue_state: QueueState,
    pending: PendingQuality,
    choice: QualityChoice,
) -> None:
    lock = user_locks.setdefault(pending.user_id, asyncio.Lock())
    async with lock:
        for index, entry in enumerate(pending.entries, 1):
            await process_url(
                message, bot, settings, database, semaphore, user_locks,
                active_jobs, queue_state, entry.url, format_id=choice.format_id,
                audio_bitrate=choice.audio_bitrate,
                quality_label=f"{choice.label} | {index}/{len(pending.entries)}",
                job_user_id=pending.user_id, manage_lock=False,
            )
        await safe_edit(message, "📚 پردازش موارد پلی‌لیست تمام شد.")


@dp.callback_query(F.data.startswith("j:"))
async def cancel_job_handler(callback: CallbackQuery, active_jobs: dict[str, ActiveJob]) -> None:
    parts = (callback.data or "").split(":")
    if len(parts) != 3 or parts[2] != "cancel":
        await callback.answer("درخواست نامعتبر.")
        return
    job = active_jobs.get(parts[1])
    if not job:
        await callback.answer("این عملیات قبلاً تمام شده است.", show_alert=True)
        return
    if callback.from_user.id != job.user_id:
        await callback.answer("این عملیات متعلق به شما نیست.", show_alert=True)
        return
    job.cancel_event.set()
    await callback.answer("در حال لغو عملیات…")


@dp.message()
async def fallback_handler(message: Message) -> None:
    await message.answer("لینک فایل یا رسانه را بفرستید. راهنما: /help")


def _format_duration(seconds: int) -> str:
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours}:{minutes:02d}:{seconds:02d}" if hours else f"{minutes}:{seconds:02d}"


async def main() -> None:
    settings = Settings.from_env()
    logging.basicConfig(level=settings.log_level, format="%(asctime)s | %(levelname)s | %(name)s | %(message)s")
    database = Database(settings.database_path)
    await database.connect()
    if settings.platform == "bale":
        from app.bale import BaleSession
        session = BaleSession(
            api=TelegramAPIServer.from_base(settings.telegram_api_root or "https://tapi.bale.ai")
        )
    else:
        session = AiohttpSession(api=TelegramAPIServer.from_base(settings.telegram_api_root)) if settings.telegram_api_root else None
    bot = Bot(
        token=settings.bot_token, session=session,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    await bot.set_my_commands([
        BotCommand(command="start", description="شروع و راهنما"),
        BotCommand(command="help", description="راهنما"),
        BotCommand(command="quota", description="سهمیه‌ی امروز"),
        BotCommand(command="about", description="درباره ما"),
        BotCommand(command="limits", description="محدودیت‌ها"),
    ])
    # Bale currently implements setMyCommands but returns 501 for profile descriptions.
    if settings.platform == "telegram":
        await bot.set_my_description(description=BOT_DESCRIPTION)
        await bot.set_my_short_description(short_description=BOT_SHORT_DESCRIPTION)

    web_server = None
    web_task = None
    if settings.web_enabled:
        from app.web import create_web_app
        config = uvicorn.Config(
            create_web_app(settings, database), host=settings.web_host,
            port=settings.web_port, log_level=settings.log_level.lower(),
        )
        web_server = uvicorn.Server(config)
        web_task = asyncio.create_task(web_server.serve())
    cleanup_task = asyncio.create_task(cleanup_shared_files(settings, database))
    logger.info(
        "Bot starting | platform=%s | max=%s | jobs=%s | web=%s | custom_api=%s | links=%s",
        settings.platform, human_size(settings.max_file_size), settings.max_concurrent_jobs,
        settings.web_enabled, bool(settings.telegram_api_root), bool(settings.public_base_url),
    )
    try:
        await dp.start_polling(
            bot,
            settings=settings,
            database=database,
            download_semaphore=asyncio.Semaphore(settings.max_concurrent_jobs),
            user_locks={}, quality_sessions={}, active_jobs={}, queue_state=QueueState(),
            allowed_updates=dp.resolve_used_update_types(),
        )
    finally:
        cleanup_task.cancel()
        if web_server:
            web_server.should_exit = True
        if web_task:
            await web_task
        await database.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass
