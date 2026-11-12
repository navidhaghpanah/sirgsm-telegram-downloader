import unittest
from types import SimpleNamespace

from aiogram.client.default import DefaultBotProperties
from aiogram.methods import SendDocument
from aiogram.types import BufferedInputFile

from app.bale import BaleSession


class BaleSessionTests(unittest.IsolatedAsyncioTestCase):
    async def test_upload_uses_direct_document_field(self):
        method = SendDocument(
            chat_id=123,
            document=BufferedInputFile(b"test", filename="sample.txt"),
            caption="caption",
        )
        bot = SimpleNamespace(default=DefaultBotProperties())
        form = BaleSession().build_form_data(bot=bot, method=method)
        names = [field[0].get("name") for field in form._fields]
        self.assertEqual(names.count("document"), 1)
        self.assertIn("chat_id", names)
        self.assertIn("caption", names)
        document_field = next(field for field in form._fields if field[0].get("name") == "document")
        chunks = [chunk async for chunk in document_field[2]]
        self.assertEqual(b"".join(chunks), b"test")


if __name__ == "__main__":
    unittest.main()
