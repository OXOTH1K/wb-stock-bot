import unittest

from app.telegram import TelegramBot


class BrandEmojiTests(unittest.IsolatedAsyncioTestCase):
    def test_plain_brand_tokens_build_custom_emoji_entities(self):
        bot = TelegramBot(
            "token",
            wb_emoji_id="111",
            ozon_emoji_id="222",
        )

        text, entities, used_custom = bot._render_brand_tokens(
            "[[WB]] WB / [[OZON]] Ozon",
            None,
        )

        self.assertEqual(text, "🟣 WB / 🔵 Ozon")
        self.assertTrue(used_custom)
        self.assertEqual(
            [entity["custom_emoji_id"] for entity in entities or []],
            ["111", "222"],
        )
        self.assertEqual(
            [entity["type"] for entity in entities or []],
            ["custom_emoji", "custom_emoji"],
        )

    def test_html_brand_tokens_use_tg_emoji_markup(self):
        bot = TelegramBot(
            "token",
            wb_emoji_id="111",
            ozon_emoji_id="222",
        )

        text, entities, used_custom = bot._render_brand_tokens(
            "[[WB]] <b>WB</b> [[OZON]]",
            "HTML",
        )

        self.assertIn(
            '<tg-emoji emoji-id="111">🟣</tg-emoji>',
            text,
        )
        self.assertIn(
            '<tg-emoji emoji-id="222">🔵</tg-emoji>',
            text,
        )
        self.assertIsNone(entities)
        self.assertTrue(used_custom)

    def test_callback_entity_restores_brand_token(self):
        bot = TelegramBot(
            "token",
            wb_emoji_id="111",
            ozon_emoji_id="222",
        )

        restored = bot._restore_brand_tokens_from_entities(
            "🟣 Новый WB FBS-заказ",
            [
                {
                    "type": "custom_emoji",
                    "offset": 0,
                    "length": 2,
                    "custom_emoji_id": "111",
                }
            ],
        )

        self.assertEqual(
            restored,
            "[[WB]] Новый WB FBS-заказ",
        )

    def test_callback_entity_restore_uses_utf16_offsets(self):
        bot = TelegramBot(
            "token",
            wb_emoji_id="111",
        )

        restored = bot._restore_brand_tokens_from_entities(
            "🙂 🟣 WB",
            [
                {
                    "type": "custom_emoji",
                    "offset": 3,
                    "length": 2,
                    "custom_emoji_id": "111",
                }
            ],
        )

        self.assertEqual(restored, "🙂 [[WB]] WB")

    async def test_callback_edit_renders_custom_emoji_again(self):
        class RecordingTelegram(TelegramBot):
            def __init__(self):
                super().__init__("token", wb_emoji_id="111")
                self.payloads = []

            async def _call(self, method, payload):
                self.payloads.append((method, dict(payload)))
                return None

        bot = RecordingTelegram()
        original = bot._restore_brand_tokens_from_entities(
            "🟣 Новый WB FBS-заказ",
            [
                {
                    "type": "custom_emoji",
                    "offset": 0,
                    "length": 2,
                    "custom_emoji_id": "111",
                }
            ],
        )

        await bot.edit_message_text(
            123,
            456,
            original + "\n\n✅ Заказ обработан.",
        )

        payload = bot.payloads[-1][1]
        self.assertEqual(
            payload["text"],
            "🟣 Новый WB FBS-заказ\n\n✅ Заказ обработан.",
        )
        self.assertEqual(
            payload["entities"][0]["custom_emoji_id"],
            "111",
        )
        self.assertEqual(
            payload["entities"][0]["offset"],
            0,
        )

    async def test_rejected_custom_emoji_retries_with_unicode_fallback(self):
        class RejectingTelegram(TelegramBot):
            def __init__(self):
                super().__init__("token", wb_emoji_id="111")
                self.payloads = []

            async def _call(self, method, payload):
                self.payloads.append((method, dict(payload)))
                if len(self.payloads) == 1:
                    raise RuntimeError("custom emoji not allowed")
                return None

        bot = RejectingTelegram()

        await bot.send_message(123, "[[WB]] WB notification")

        self.assertEqual(len(bot.payloads), 2)
        first = bot.payloads[0][1]
        second = bot.payloads[1][1]
        self.assertIn("entities", first)
        self.assertNotIn("entities", second)
        self.assertEqual(second["text"], "🟣 WB notification")


if __name__ == "__main__":
    unittest.main()
