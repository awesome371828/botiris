from aiogram import Router, types, F
from aiogram.utils.chat_action import ChatActionSender
from gigachat import GigaChat
from config import GIGACHAT_AUTH_KEY
import asyncio

router = Router()

# Инициализация GigaChat клиента
giga = None
if GIGACHAT_AUTH_KEY:
    try:
        giga = GigaChat(
            credentials=GIGACHAT_AUTH_KEY,
            scope="GIGACHAT_API_PERS",
            verify_ssl_certs=False,
            model="GigaChat-3-Ultra",
        )
    except Exception as e:
        print(f"GigaChat init error: {e}")


@router.message(F.text & ~F.text.startswith(("/", ".", "!")))
async def ai_chat_handler(message: types.Message):
    """Отвечает на сообщения через GigaChat с индикатором 'печатает...'"""
    if not giga:
        return

    # Проверяем, является ли сообщение командой модерации
    lower_text = message.text.lower()
    command_starts = (
        "мут", "размут", "бан", "разбан", "кик", "варн", "унварн",
        "повысить", "понизить", "мойранг", "варны", "преды",
        "mute", "unmute", "ban", "unban", "kick", "warn", "unwarn",
    )
    if lower_text.startswith(command_starts):
        return

    # Отправляем статус "печатает..." пока GigaChat думает
    async with ChatActionSender.typing(bot=message.bot, chat_id=message.chat.id):
        try:
            prompt = f"Ответь кратко и по делу на сообщение пользователя: {message.text}"
            response = await asyncio.to_thread(giga.chat, prompt)
            answer = response.messages[0].content
            await message.reply(answer)
        except Exception as e:
            await message.reply(f"❌ Ошибка GigaChat: {e}")
