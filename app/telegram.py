from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Awaitable, Callable, Iterable
from typing import Any

import aiohttp

log = logging.getLogger(__name__)

MessageHandler = Callable[[int, str], Awaitable[None]]
CallbackHandler = Callable[[int, int, str, str], Awaitable[None]]


class TelegramBot:
    _BRAND_TOKEN_RE = re.compile(r"\[\[(WB|OZON)\]\]")

    def __init__(
        self,
        token: str,
        timeout: int = 40,
        *,
        wb_emoji_id: str = "",
        ozon_emoji_id: str = "",
    ):
        self.base_url = f"https://api.telegram.org/bot{token}"
        self._timeout = aiohttp.ClientTimeout(total=timeout)
        self._session: aiohttp.ClientSession | None = None
        self._offset = 0
        self._brand_emoji = {
            "WB": ("🟣", str(wb_emoji_id or "").strip()),
            "OZON": ("🔵", str(ozon_emoji_id or "").strip()),
        }

    @staticmethod
    def _utf16_len(value: str) -> int:
        return len(value.encode("utf-16-le")) // 2

    def _render_brand_tokens(
        self,
        text: str,
        parse_mode: str | None,
        *,
        use_custom: bool = True,
    ) -> tuple[str, list[dict[str, Any]] | None, bool]:
        """Render [[WB]] / [[OZON]] as brand markers.

        With configured Telegram custom-emoji IDs, plain messages use entity
        offsets and HTML messages use <tg-emoji>. Without IDs, or on fallback,
        the tokens become stable Unicode markers.
        """
        mode = str(parse_mode or "").upper()
        if mode == "HTML":
            used_custom = False

            def repl(match: re.Match[str]) -> str:
                nonlocal used_custom
                fallback, emoji_id = self._brand_emoji[match.group(1)]
                if use_custom and emoji_id:
                    used_custom = True
                    return (
                        f'<tg-emoji emoji-id="{emoji_id}">'
                        f"{fallback}</tg-emoji>"
                    )
                return fallback

            return (
                self._BRAND_TOKEN_RE.sub(repl, text),
                None,
                used_custom,
            )

        parts: list[str] = []
        entities: list[dict[str, Any]] = []
        cursor = 0
        used_custom = False
        for match in self._BRAND_TOKEN_RE.finditer(text):
            parts.append(text[cursor : match.start()])
            fallback, emoji_id = self._brand_emoji[match.group(1)]
            prefix = "".join(parts)
            offset = self._utf16_len(prefix)
            parts.append(fallback)
            if use_custom and emoji_id and not parse_mode:
                used_custom = True
                entities.append(
                    {
                        "type": "custom_emoji",
                        "offset": offset,
                        "length": self._utf16_len(fallback),
                        "custom_emoji_id": emoji_id,
                    }
                )
            cursor = match.end()
        parts.append(text[cursor:])
        return "".join(parts), (entities or None), used_custom

    async def __aenter__(self) -> "TelegramBot":
        self._session = aiohttp.ClientSession(timeout=self._timeout)
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        if self._session:
            await self._session.close()

    @property
    def session(self) -> aiohttp.ClientSession:
        if self._session is None:
            raise RuntimeError("TelegramBot must be used as an async context manager")
        return self._session

    async def _call(self, method: str, payload: dict):
        async with self.session.post(f"{self.base_url}/{method}", json=payload) as response:
            data = await response.json(content_type=None)
            if response.status >= 400 or not data.get("ok"):
                raise RuntimeError(f"Telegram API error: {data}")
            return data.get("result")

    async def send_message(
        self,
        chat_id: int,
        text: str,
        *,
        parse_mode: str | None = None,
        reply_markup: dict[str, Any] | None = None,
    ) -> None:
        # Formatted stock pages contain HTML tags and are deliberately kept below
        # Telegram's 4096-character limit so tags are never split between messages.
        if parse_mode:
            if len(text) > 4096:
                raise ValueError("Formatted Telegram message exceeds 4096 characters")
            chunks = [text]
        else:
            chunks = split_text(text, 3900)

        for index, chunk in enumerate(chunks):
            rendered, entities, used_custom = self._render_brand_tokens(
                chunk, parse_mode
            )
            payload: dict[str, Any] = {
                "chat_id": chat_id,
                "text": rendered,
                "link_preview_options": {"is_disabled": True},
            }
            if parse_mode:
                payload["parse_mode"] = parse_mode
            elif entities:
                payload["entities"] = entities
            # Attach buttons to the final chunk only.
            if reply_markup is not None and index == len(chunks) - 1:
                payload["reply_markup"] = reply_markup
            try:
                await self._call("sendMessage", payload)
            except RuntimeError:
                if not used_custom:
                    raise
                log.warning(
                    "Telegram custom emoji rejected; retrying with Unicode fallback"
                )
                fallback, _, _ = self._render_brand_tokens(
                    chunk, parse_mode, use_custom=False
                )
                payload["text"] = fallback
                payload.pop("entities", None)
                await self._call("sendMessage", payload)

    async def edit_message_text(
        self,
        chat_id: int,
        message_id: int,
        text: str,
        *,
        parse_mode: str | None = None,
        reply_markup: dict[str, Any] | None = None,
    ) -> None:
        rendered, entities, used_custom = self._render_brand_tokens(
            text, parse_mode
        )
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": rendered,
            "link_preview_options": {"is_disabled": True},
        }
        if parse_mode:
            payload["parse_mode"] = parse_mode
        elif entities:
            payload["entities"] = entities
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        try:
            await self._call("editMessageText", payload)
        except RuntimeError:
            if not used_custom:
                raise
            log.warning(
                "Telegram custom emoji rejected while editing; "
                "retrying with Unicode fallback"
            )
            fallback, _, _ = self._render_brand_tokens(
                text, parse_mode, use_custom=False
            )
            payload["text"] = fallback
            payload.pop("entities", None)
            await self._call("editMessageText", payload)

    async def answer_callback_query(self, callback_query_id: str) -> None:
        await self._call("answerCallbackQuery", {"callback_query_id": callback_query_id})

    async def edit_message_reply_markup(
        self,
        chat_id: int,
        message_id: int,
        reply_markup: dict[str, Any] | None = None,
    ) -> None:
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "message_id": message_id,
            "reply_markup": reply_markup or {"inline_keyboard": []},
        }
        await self._call("editMessageReplyMarkup", payload)

    async def broadcast(
        self,
        chat_ids: Iterable[int],
        text: str,
        *,
        parse_mode: str | None = None,
        reply_markup: dict[str, Any] | None = None,
    ) -> None:
        for chat_id in chat_ids:
            try:
                await self.send_message(
                    chat_id, text, parse_mode=parse_mode, reply_markup=reply_markup
                )
            except Exception:
                log.exception("Failed to send Telegram message to %s", chat_id)

    async def polling_loop(
        self,
        handler: MessageHandler,
        callback_handler: CallbackHandler | None = None,
    ) -> None:
        while True:
            try:
                updates = await self._call(
                    "getUpdates",
                    {
                        "offset": self._offset,
                        "timeout": 25,
                        "allowed_updates": ["message", "callback_query"],
                    },
                )
                for update in updates or []:
                    self._offset = max(self._offset, int(update["update_id"]) + 1)

                    callback = update.get("callback_query") or {}
                    if callback:
                        callback_id = callback.get("id")
                        if callback_id:
                            # Telegram clients keep a loading indicator visible until
                            # answerCallbackQuery is called, even when no toast is needed.
                            await self.answer_callback_query(str(callback_id))

                        if callback_handler is not None:
                            message = callback.get("message") or {}
                            chat = message.get("chat") or {}
                            chat_id = chat.get("id")
                            message_id = message.get("message_id")
                            data = callback.get("data")
                            message_text = str(message.get("text") or "")
                            if chat_id is not None and message_id is not None and data:
                                await callback_handler(
                                    int(chat_id), int(message_id), str(data), message_text
                                )
                        continue

                    message = update.get("message") or {}
                    text = message.get("text")
                    chat = message.get("chat") or {}
                    chat_id = chat.get("id")
                    if text and chat_id is not None:
                        await handler(int(chat_id), str(text).strip())
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Telegram polling error")
                await asyncio.sleep(3)


def split_text(text: str, limit: int) -> list[str]:
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    current: list[str] = []
    current_len = 0
    for line in text.splitlines(keepends=True):
        if current and current_len + len(line) > limit:
            chunks.append("".join(current).rstrip())
            current = []
            current_len = 0
        if len(line) > limit:
            if current:
                chunks.append("".join(current).rstrip())
                current = []
                current_len = 0
            for pos in range(0, len(line), limit):
                chunks.append(line[pos : pos + limit].rstrip())
            continue
        current.append(line)
        current_len += len(line)
    if current:
        chunks.append("".join(current).rstrip())
    return chunks
