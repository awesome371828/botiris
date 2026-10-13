import asyncio
import logging
from aiogram import Bot, Dispatcher, types, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton
from config import BOT_TOKEN
from database.crud import create_user, get_user
from handlers import moderation

logging.basicConfig(level=logging.INFO)

bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()

dp.include_router(moderation.router)


def main_keyboard():
    kb = ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📋 Меню"), KeyboardButton(text="👤 Профиль")],
            [KeyboardButton(text="⚠️ Варны"), KeyboardButton(text="🛡️ Права")],
            [KeyboardButton(text="📊 Топ"), KeyboardButton(text="❓ Помощь")],
        ],
        resize_keyboard=True,
    )
    return kb


@dp.message(F.text == "/start")
async def cmd_start(message: types.Message):
    if message.chat.type == "private":
        await message.answer(
            "Привет! Я бот-модератор.\n"
            "Добавь меня в группу и назначь администратором.",
            reply_markup=main_keyboard(),
        )
    else:
        await message.answer(f"Бот активирован в чате {message.chat.title}!")


@dp.message(F.text == "📋 Меню")
async def btn_menu(message: types.Message):
    await message.answer("Главное меню:", reply_markup=main_keyboard())


@dp.message(F.text == "👤 Профиль")
async def btn_profile(message: types.Message):
    user = get_user(message.from_user.id, message.chat.id)
    if not user:
        create_user(message.from_user.id, message.chat.id, message.from_user.username, message.from_user.first_name)
        user = get_user(message.from_user.id, message.chat.id)
    await message.answer(
        f"👤 {message.from_user.mention_html()}\n"
        f"⚠️ Варны: {user['warns']}\n"
        f"🏆 Ранг: {user['rank']}"
    )


@dp.message(F.text == "⚠️ Варны")
async def btn_warns(message: types.Message):
    user = get_user(message.from_user.id, message.chat.id)
    if user:
        await message.answer(f"⚠️ У вас {user['warns']} варнов.")
    else:
        await message.answer("⚠️ У вас нет варнов.")


@dp.message(F.text == "🛡️ Права")
async def btn_perms(message: types.Message):
    await message.answer("Напишите: <code>права</code> или <code>права @user</code>")


@dp.message(F.text == "📊 Топ")
async def btn_top(message: types.Message):
    await message.answer("Напишите: <code>топ</code>")


@dp.message(F.text == "❓ Помощь")
async def btn_help(message: types.Message):
    help_text = (
        "<b>📋 Команды модерации:</b>\n\n"
        "<b>Базовые:</b>\n"
        "• мут @user 10м — замутить\n"
        "• размут / анмут @user — снять мут\n"
        "• бан @user — забанить\n"
        "• разбан @user — разбанить\n"
        "• кик @user — кикнуть\n"
        "• варн @user — предупреждение\n"
        "• унварн / анварн @user — снять варны\n"
        "• варны @user — проверить варны\n\n"
        "<b>Сроки:</b> 10с, 5м, 1 час, 1 день, 1 неделя, 1 месяц, 1 год\n\n"
        "<b>Управление:</b>\n"
        "• !модер — назначить модератора\n"
        "• повысить / понизить\n"
        "• снять модера — снять права\n\n"
        "<b>Сообщения:</b>\n"
        "• -смс — удалить сообщение\n"
        "• .пин / .анпин — закрепить / открепить\n"
        "• очистить 10 — удалить 10 сообщений\n\n"
        "<b>Информация:</b>\n"
        "• инфо @user / кто админ / кто модер\n"
        "• топ / стата / логи / отчёт\n\n"
        "<b>Фильтры:</b>\n"
        "• +антиссылка / -антиссылка\n"
        "• +антимат / -антимат\n"
        "• +антифлуд / -антифлуд\n\n"
        "<b>Триггеры:</b>\n"
        "• триггер варн 3 мут 1ч\n"
        "• триггеры — список триггеров"
    )
    await message.answer(help_text)


@dp.message(F.new_chat_members)
async def greet_new_member(message: types.Message):
    for new_member in message.new_chat_members:
        if new_member.is_bot:
            continue
        if not get_user(new_member.id, message.chat.id):
            create_user(new_member.id, message.chat.id, new_member.username, new_member.first_name)
        await message.answer(f"👋 Привет, {new_member.mention_html()}!")


async def main():
    print("Бот запущен...")
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
