from aiogram import Router, types
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
            model="GigaChat-2-Max"
        )
    except Exception as e:
        print(f"GigaChat init error: {e}")


async def check_with_giga(text: str) -> bool:
    """Проверяет, является ли текст командой модерации."""
    if not giga:
        return True  # Если GigaChat недоступен, пропускаем все команды
    
    prompt = f"""Определи, является ли следующее сообщение командой модерации для Telegram-бота.
Команды модерации: мут, размут, бан, разбан, кик, варн, снять варн, повысить, понизить.
Ответь ТОЛЬКО одним словом: ДА или НЕТ.
Сообщение: "{text}"
Ответ:"""
    
    try:
        response = await asyncio.to_thread(giga.chat, prompt)
        answer = response.messages[0].content.strip().upper()
        return "ДА" in answer or "YES" in answer
    except Exception as e:
        print(f"GigaChat check error: {e}")
        return True  # При ошибке пропускаем


@router.message()
async def ai_filter(message: types.Message):
    """Проверка через GigaChat ПЕРЕД обработкой команд."""
    if not message.text:
        return
    
    # Быстрая проверка: если текст начинается с ключевых слов команд — пропускаем
    lower_text = message.text.lower()
    command_starts = (
        "мут", "размут", "бан", "разбан", "кик", "варн", "унварн", "анварн",
        "повысить", "понизить", "мойранг", "варны", "преды", "warns",
        ".мут", "!мут", "/мут", "mute", ".ban", "!ban", "ban", "unban",
        "unwarn", "anwarn", "promote", "demote", "rank", "warns"
    )
    
    # Если команда начинается с известного префикса — пропускаем GigaChat
    if lower_text.startswith(command_starts):
        return
    
    # Для остальных сообщений — проверяем через GigaChat (только если есть ключ)
    # Это опционально и может замедлить бота
    # await check_with_giga(message.text)
