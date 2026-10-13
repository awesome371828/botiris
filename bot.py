import asyncio
import logging
from aiogram import Bot, Dispatcher, types, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton
from config import BOT_TOKEN
from database.crud import create_user, get_user
from handlers import moderation

logging.basicConfig(level=logging.INFO)

bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()

dp.include_router(moderation.router)


def get_main_keyboard():
    """Кнопки управления."""
    kb = ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📋 Меню"), KeyboardButton(text="👤 Профиль")],
            [KeyboardButton(text="⚠️ Варны"), KeyboardButton(text="🔇 Мут")],
            [KeyboardButton(text="❓ Помощь")]
        ],
        resize_keyboard=True
    )
    return kb


@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    if message.chat.type == "private":
        await message.answer(
            "Привет! Я бот-модератор.\n"
            "Добавь меня в группу и назначь администратором.",
            reply_markup=get_main_keyboard()
        )
    else:
        await message.answer(f"Бот активирован в чате {message.chat.title}!")


@dp.message(Command("help"))
async def cmd_help(message: types.Message):
    help_text = (
        "<b>📋 Команды модерации:</b>\n\n"
        "<b>Русские:</b>\n"
        "• мут @user 10м — замутить на 10 минут\n"
        "• мут @user 1ч — замутить на 1 час\n"
        "• мут @user 1д — замутить на 1 день\n"
        "• мут @user 1мес — замутить на 1 месяц\n"
        "• мут @user 1г — замутить на 1 год\n"
        "• мут @user — бессрочно\n"
        "• размут @user — снять мут\n"
        "• бан @user — забанить\n"
        "• разбан @user — разбанить\n"
        "• кик @user — кикнуть\n"
        "• варн @user — выдать предупреждение\n"
        "• унварн @user / анварн @user — снять варны\n"
        "• варны @user — посмотреть варны\n"
        "• повысить @user — повысить ранг\n"
        "• понизить @user — понизить ранг\n"
        "• мойранг — мой ранг\n\n"
        "<b>English:</b>\n"
        "• mute @user 10m — mute for 10 minutes\n"
        "• unmute @user — unmute\n"
        "• ban @user — ban\n"
        "• unban @user — unban\n"
        "• kick @user — kick\n"
        "• warn @user — warn\n"
        "• unwarn @user — remove warns"
    )
    await message.answer(help_text)


@dp.message(F.text == "📋 Меню")
async def btn_menu(message: types.Message):
    await message.answer("Главное меню:", reply_markup=get_main_keyboard())


@dp.message(F.text == "👤 Профиль")
async def btn_profile(message: types.Message):
    user = get_user(message.from_user.id, message.chat.id)
    if not user:
        create_user(message.from_user.id, message.chat.id, message.from_user.username, message.from_user.first_name)
        user = get_user(message.from_user.id, message.chat.id)
    await message.answer(f"👤 {message.from_user.mention_html()}\n"
                        f"💰 Ириски: {user['balance']}\n"
                        f"⚠️ Варны: {user['warns']}\n"
                        f"🏆 Ранг: {user['rank']}")


@dp.message(F.text == "⚠️ Варны")
async def btn_warns(message: types.Message):
    user = get_user(message.from_user.id, message.chat.id)
    if user:
        await message.answer(f"⚠️ У вас {user['warns']} варнов.")
    else:
        await message.answer("⚠️ У вас нет варнов.")


@dp.message(F.text == "🔇 Мут")
async def btn_mute(message: types.Message):
    await message.answer("Напишите: <code>мут @user 10м</code>\n"
                        "Сроки: 10с, 10м, 10ч, 10д, 1мес, 1г")


@dp.message(F.text == "❓ Помощь")
async def btn_help(message: types.Message):
    await cmd_help(message)


@dp.message(F.new_chat_members)
async def greet_new_member(message: types.Message):
    for new_member in message.new_chat_members:
        if new_member.is_bot:
            continue
        if not get_user(new_member.id, message.chat.id):
            create_user(new_member.id, message.chat.id, new_member.username, new_member.first_name)
        await message.answer(f"👋 Привет, {new_member.mention_html()}! Добро пожаловать.")


async def main():
    print("Бот запущен...")
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
