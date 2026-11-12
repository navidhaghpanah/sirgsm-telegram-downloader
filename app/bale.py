"""Compatibility adapters for Bale's Telegram-like Bot API."""

from __future__ import annotations

from aiohttp import FormData
from aiogram import Bot
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.methods.base import TelegramMethod, TelegramType
from aiogram.types import InputFile


class BaleSession(AiohttpSession):
    """Send top-level files directly instead of using Telegram's attach:// syntax.

    Bale accepts multipart uploads but currently treats an ``attach://`` value as
    an HTTP URL. aiogram otherwise handles polling, callbacks and cached file_ids
    without modification.
    """

    def build_form_data(self, bot: Bot, method: TelegramMethod[TelegramType]) -> FormData:
        form = FormData(quote_fields=False)
        files: dict[str, InputFile] = {}
        for key, value in method.model_dump(warnings=False).items():
            if isinstance(value, InputFile):
                form.add_field(key, value.read(bot), filename=value.filename or key)
                continue
            value = self.prepare_value(value, bot=bot, files=files)
            if not value:
                continue
            form.add_field(key, value)
        # No current Bale method used by the bot nests InputFile objects. Keep
        # aiogram's fallback for forward compatibility with simple extra files.
        for key, value in files.items():
            form.add_field(key, value.read(bot), filename=value.filename or key)
        return form
