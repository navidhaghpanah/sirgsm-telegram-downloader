from __future__ import annotations

import asyncio
import html
import logging

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from app.config import Settings
from app.database import Database
from app.downloader import human_size

logger = logging.getLogger(__name__)
router = Router(name="admin")


def is_admin(user_id: int, settings: Settings) -> bool:
    return user_id in settings.admin_ids


def admin_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📊 آمار", callback_data="admin:stats"),
         InlineKeyboardButton(text="👥 کاربران", callback_data="admin:users")],
        [InlineKeyboardButton(text="🧾 Jobهای اخیر", callback_data="admin:jobs")],
    ])


async def stats_text(database: Database) -> str:
    stats = await database.stats()
    return (
        "📊 <b>آمار ربات</b>\n\n"
        f"👥 کاربران: <b>{stats['users']}</b>\n"
        f"🚫 مسدودها: <b>{stats['blocked']}</b>\n"
        f"📥 دانلودها: <b>{stats['downloads']}</b>\n"
        f"📦 ترافیک: <b>{human_size(stats['bytes'])}</b>\n"
        f"⚡ فایل‌های کش‌شده: <b>{stats['cached']}</b>\n"
        f"🧾 Jobها: <b>{stats['jobs']}</b>"
    )


@router.message(Command("admin"))
async def admin_panel(message: Message, settings: Settings, database: Database) -> None:
    if not message.from_user or not is_admin(message.from_user.id, settings):
        return
    await message.answer("🛠 <b>پنل مدیریت</b>\nیک بخش را انتخاب کنید:", reply_markup=admin_keyboard())
    await message.answer(await stats_text(database))


@router.callback_query(F.data.startswith("admin:"))
async def admin_callbacks(callback: CallbackQuery, settings: Settings, database: Database) -> None:
    if not is_admin(callback.from_user.id, settings):
        await callback.answer("دسترسی ندارید.", show_alert=True)
        return
    action = (callback.data or "").split(":", 1)[1]
    if action == "stats":
        await callback.message.answer(await stats_text(database))
    elif action == "users":
        users = await database.list_users(20)
        lines = ["👥 <b>کاربران اخیر</b>"]
        for user in users:
            name = html.escape(user["first_name"] or "بدون نام")
            lines.append(
                f"{'🚫' if user['blocked'] else '✅'} <code>{user['user_id']}</code> | {name} | "
                f"{user['total_downloads']} دانلود"
            )
        await callback.message.answer("\n".join(lines))
    elif action == "jobs":
        jobs = await database.recent_jobs(15)
        lines = ["🧾 <b>Jobهای اخیر</b>"]
        for job in jobs:
            lines.append(
                f"<code>{job['id'][:8]}</code> | <code>{job['user_id']}</code> | "
                f"{html.escape(job['quality'] or '-')} | {html.escape(job['status'])}"
            )
        await callback.message.answer("\n".join(lines))
    await callback.answer()


@router.message(Command("block"))
async def block_user(message: Message, settings: Settings, database: Database) -> None:
    if not message.from_user or not is_admin(message.from_user.id, settings):
        return
    try:
        user_id = int((message.text or "").split()[1])
    except (IndexError, ValueError):
        await message.answer("استفاده: <code>/block USER_ID</code>")
        return
    await database.set_blocked(user_id, True)
    await message.answer(f"🚫 کاربر <code>{user_id}</code> مسدود شد.")


@router.message(Command("unblock"))
async def unblock_user(message: Message, settings: Settings, database: Database) -> None:
    if not message.from_user or not is_admin(message.from_user.id, settings):
        return
    try:
        user_id = int((message.text or "").split()[1])
    except (IndexError, ValueError):
        await message.answer("استفاده: <code>/unblock USER_ID</code>")
        return
    await database.set_blocked(user_id, False)
    await message.answer(f"✅ کاربر <code>{user_id}</code> آزاد شد.")


@router.message(Command("setquota"))
async def set_quota(message: Message, settings: Settings, database: Database) -> None:
    if not message.from_user or not is_admin(message.from_user.id, settings):
        return
    try:
        _, raw_user, raw_count, raw_mb = (message.text or "").split()
        user_id, count, megabytes = int(raw_user), int(raw_count), int(raw_mb)
        if count <= 0 or megabytes <= 0:
            raise ValueError
    except ValueError:
        await message.answer("استفاده: <code>/setquota USER_ID COUNT MB</code>\nمثال: <code>/setquota 123456 20 1000</code>")
        return
    await database.set_quota(user_id, count, megabytes * 1024 * 1024)
    await message.answer(f"✅ سهمیه‌ی <code>{user_id}</code> روی {count} دانلود و {megabytes} مگابایت تنظیم شد.")


@router.message(Command("broadcast"))
async def broadcast(message: Message, bot: Bot, settings: Settings, database: Database) -> None:
    if not message.from_user or not is_admin(message.from_user.id, settings):
        return
    text = (message.text or "").partition(" ")[2].strip()
    if not text:
        await message.answer("استفاده: <code>/broadcast متن پیام</code>")
        return
    users = await database.list_users(100000)
    sent = failed = 0
    status = await message.answer(f"📣 ارسال به {len(users)} کاربر آغاز شد…")
    for user in users:
        if user["blocked"]:
            continue
        try:
            await bot.send_message(user["user_id"], text)
            sent += 1
        except TelegramAPIError:
            failed += 1
        await asyncio.sleep(0.05)
    await status.edit_text(f"📣 پایان ارسال\n✅ موفق: {sent}\n❌ ناموفق: {failed}")
