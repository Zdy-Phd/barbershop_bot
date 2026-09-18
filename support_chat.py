"""
МОДУЛЬ: ЖИВОЙ ДИАЛОГ ЧЕРЕЗ БОТА (aiogram 3.x)
================================================
Позволяет тебе переписываться с клиентами ЧЕРЕЗ бота, как оператор:
  - Любое сообщение клиента боту пересылается тебе (админу)
  - Ты отвечаешь (Reply) на это сообщение в своём чате с ботом
  - Бот автоматически понимает, какому клиенту отправить твой ответ
s
Как подключить к существующему боту (barbershop_bot.py / restaurant_bot.py):
  1. Скопируй код ниже в свой файл (или импортируй как отдельный router).
  2. Добавь router.include_router(support_router) при регистрации роутеров.
  3. В .env укажи ADMIN_CHAT_ID — твой личный chat_id.

Важно: этот router должен подключаться ПОСЛЕДНИМ (после роутеров с FSM-логикой
записи/заказа), иначе он будет перехватывать сообщения, которые должны идти
в другие обработчики.
"""

import os

from aiogram import Bot, F, Router
from aiogram.types import Message

support_router = Router()

ADMIN_CHAT_ID = int(os.getenv("ADMIN_CHAT_ID", "7657045241"))

# Связь "id пересланного сообщения у админа" -> "chat_id клиента"
# Для реального проекта лучше вынести в БД, но для демо хватает памяти.
FORWARDED_MESSAGES: dict[int, int] = {}


@support_router.message(F.chat.id != ADMIN_CHAT_ID)
async def forward_to_admin(message: Message, bot: Bot) -> None:
    """Любое сообщение от клиента — пересылаем админу."""
    if not ADMIN_CHAT_ID:
        return

    forwarded = await bot.forward_message(
        chat_id=ADMIN_CHAT_ID,
        from_chat_id=message.chat.id,
        message_id=message.message_id,
    )
    # Запоминаем: если админ ответит на ЭТО пересланное сообщение,
    # значит ответ нужно отправить именно этому клиенту.
    FORWARDED_MESSAGES[forwarded.message_id] = message.chat.id

    await bot.send_message(
        ADMIN_CHAT_ID,
        f"👆 Сообщение от {message.from_user.full_name} "
        f"(@{message.from_user.username or 'без username'}). "
        f"Ответь на него (Reply), чтобы написать клиенту.",
        reply_to_message_id=forwarded.message_id,
    )


@support_router.message(F.chat.id == ADMIN_CHAT_ID, F.reply_to_message)
async def admin_reply(message: Message, bot: Bot) -> None:
    """Ответ админа на пересланное сообщение — уходит клиенту."""
    replied_id = message.reply_to_message.message_id
    client_chat_id = FORWARDED_MESSAGES.get(replied_id)

    if client_chat_id is None:
        return  # это не ответ на пересланное сообщение клиента

    await bot.send_message(client_chat_id, message.text or "")
    await message.answer("✅ Отправлено клиенту")
