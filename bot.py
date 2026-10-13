import asyncio
import logging
from aiogram import Bot, Dispatcher, types, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from config import BOT_TOKEN
from database.crud import create_user, get_user
from handlers import moderation, economy, ranks, social, rewards

logging.basicConfig(level=logging.INFO)

bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()

dp.include_router(moderation.router)
dp.include_router(economy.router)
dp.include_router(ranks.router)
dp.include_router(social.router)
dp.include_router(rewards.router)


@dp.message(F.new_chat_members)
async def greet_new_member(message: types.Message):
    for new_member in message.new_chat_members:
        if new_member.is_bot:
            continue
        if not get_user(new_member.id, message.chat.id):
            create_user(new_member.id, message.chat.id, new_member.username, new_member.first_name)
        await message.answer(f"👋 Привет, {new_member.mention_html()}! Добро пожаловать.")


@dp.message(F.text == "/start")
async def cmd_start(message: types.Message):
    if message.chat.type == "private":
        await message.answer("Привет! Добавь меня в группу, чтобы я начал работать.")
    else:
        await message.answer(f"Бот активирован в чате {message.chat.title}!")


async def main():
    print("Бот запущен...")
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
