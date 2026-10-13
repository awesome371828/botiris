import os
import logging
import asyncio
import random
import sqlite3
from datetime import datetime, timedelta

from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command, CommandStart
from aiogram.types import ChatPermissions
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties

# --- НАСТРОЙКИ ---
# Токен берется из переменных окружения (GitHub Secrets)
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")

if not BOT_TOKEN:
    print("Ошибка: Переменная TELEGRAM_BOT_TOKEN не найдена!")
    exit(1)

# Инициализация бота и диспетчера
bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()

# Настройка логирования
logging.basicConfig(level=logging.INFO)

# --- БАЗА ДАННЫХ (SQLite для простоты) ---
DB_NAME = "bot_database.db"

def init_db():
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    # Таблица пользователей
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER,
            chat_id INTEGER,
            username TEXT,
            balance INTEGER DEFAULT 0,
            warns INTEGER DEFAULT 0,
            last_bonus TEXT,
            PRIMARY KEY (user_id, chat_id)
        )
    """)
    # Таблица настроек чата
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS chats (
            chat_id INTEGER PRIMARY KEY,
            welcome_text TEXT DEFAULT 'Добро пожаловать, {name}!',
            rules TEXT DEFAULT 'Правила не установлены.'
        )
    """)
    conn.commit()
    conn.close()

# Функции работы с БД
def get_user(user_id, chat_id):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM users WHERE user_id = ? AND chat_id = ?", (user_id, chat_id))
    user = cursor.fetchone()
    if not user:
        cursor.execute("INSERT INTO users (user_id, chat_id, username) VALUES (?, ?, ?)", (user_id, chat_id, "Unknown"))
        conn.commit()
        cursor.execute("SELECT * FROM users WHERE user_id = ? AND chat_id = ?", (user_id, chat_id))
        user = cursor.fetchone()
    conn.close()
    return user

def update_balance(user_id, chat_id, amount):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("UPDATE users SET balance = balance + ? WHERE user_id = ? AND chat_id = ?", (amount, user_id, chat_id))
    conn.commit()
    conn.close()

def add_warn(user_id, chat_id):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("UPDATE users SET warns = warns + 1 WHERE user_id = ? AND chat_id = ?", (user_id, chat_id))
    conn.commit()
    cursor.execute("SELECT warns FROM users WHERE user_id = ? AND chat_id = ?", (user_id, chat_id))
    warns = cursor.fetchone()[0]
    conn.close()
    return warns

# --- ХЭНДЛЕРЫ (КОМАНДЫ) ---

@dp.message(CommandStart())
async def cmd_start(message: types.Message):
    if message.chat.type == "private":
        await message.answer("Привет! Я бот-менеджер. Добавь меня в группу, чтобы я начал работать.")
    else:
        await message.answer(f"Бот активирован в чате {message.chat.title}!")

@dp.message(Command("help"))
async def cmd_help(message: types.Message):
    help_text = (
        "<b>Доступные команды:</b>\n"
        "/start - Запуск бота\n"
        "/help - Список команд\n"
        "/balance - Показать баланс ирисок\n"
        "/bonus - Получить ежедневный бонус\n"
        "/duel @user - Дуэль на ириски\n"
        "/warn @user - Выдать предупреждение (для админов)\n"
        "/mute @user - Замутить пользователя (для админов)\n"
        "/unmute @user - Размутить пользователя (для админов)\n"
    )
    await message.answer(help_text)

@dp.message(Command("balance"))
async def cmd_balance(message: types.Message):
    user = get_user(message.from_user.id, message.chat.id)
    balance = user[3]
    await message.answer(f"💰 Ваш баланс: <b>{balance}</b> ирисок.")

@dp.message(Command("bonus"))
async def cmd_bonus(message: types.Message):
    user_id = message.from_user.id
    chat_id = message.chat.id
    user = get_user(user_id, chat_id)
    last_bonus = user[4]
    
    now = datetime.now()
    
    if last_bonus:
        last_bonus_date = datetime.fromisoformat(last_bonus)
        if now - last_bonus_date < timedelta(hours=24):
            time_left = timedelta(hours=24) - (now - last_bonus_date)
            hours, remainder = divmod(time_left.seconds, 3600)
            minutes, _ = divmod(remainder, 60)
            await message.reply(f"⏳ Вы уже получали бонус. Приходите через {hours} ч. {minutes} мин.")
            return

    bonus_amount = random.randint(10, 50)
    update_balance(user_id, chat_id, bonus_amount)
    
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("UPDATE users SET last_bonus = ? WHERE user_id = ? AND chat_id = ?", (now.isoformat(), user_id, chat_id))
    conn.commit()
    conn.close()
    
    await message.reply(f"🎁 Вы получили ежедневный бонус: <b>{bonus_amount}</b> ирисок!")

@dp.message(Command("duel"))
async def cmd_duel(message: types.Message):
    if not message.reply_to_message:
        await message.reply("Ответьте на сообщение пользователя, чтобы вызвать его на дуэль!")
        return
    
    opponent = message.reply_to_message.from_user
    if opponent.id == message.from_user.id:
        await message.reply("Нельзя вызвать самого себя!")
        return
        
    if opponent.is_bot:
        await message.reply("С ботами не duelимся!")
        return

    # Проверка баланса
    user1 = get_user(message.from_user.id, message.chat.id)
    user2 = get_user(opponent.id, message.chat.id)
    
    if user1[3] < 10 or user2[3] < 10:
        await message.reply("У одного из участников недостаточно ирисок (нужно минимум 10).")
        return

    winner = random.choice([message.from_user, opponent])
    loser = opponent if winner.id == message.from_user.id else message.from_user
    
    update_balance(winner.id, message.chat.id, 10)
    update_balance(loser.id, message.chat.id, -10)
    
    await message.answer(f"⚔️ {message.from_user.mention_html()} вызвал {opponent.mention_html()} на дуэль!\n"
                         f"Победитель: {winner.mention_html()} и забирает 10 ирисок!")

@dp.message(Command("warn"))
async def cmd_warn(message: types.Message):
    # Проверка прав (простая)
    member = await message.chat.get_member(message.from_user.id)
    if not member.status in ["administrator", "creator"]:
        await message.reply("У вас нет прав для этой команды.")
        return

    if not message.reply_to_message:
        await message.reply("Ответьте на сообщение нарушителя.")
        return

    target = message.reply_to_message.from_user
    warns = add_warn(target.id, message.chat.id)
    
    await message.reply(f"⚠️ Пользователь {target.mention_html()} получил предупреждение. Всего: {warns}/3")
    
    if warns >= 3:
        try:
            await bot.restrict_chat_member(
                chat_id=message.chat.id,
                user_id=target.id,
                permissions=ChatPermissions(can_send_messages=False),
                until_date=datetime.now() + timedelta(hours=1)
            )
            await message.answer(f"🔨 {target.mention_html()} замучен на 1 час за 3 предупреждения.")
            # Сброс варнов
            conn = sqlite3.connect(DB_NAME)
            cursor = conn.cursor()
            cursor.execute("UPDATE users SET warns = 0 WHERE user_id = ? AND chat_id = ?", (target.id, message.chat.id))
            conn.commit()
            conn.close()
        except Exception as e:
            await message.answer(f"Не удалось замутить: {e}")

@dp.message(Command("mute"))
async def cmd_mute(message: types.Message):
    member = await message.chat.get_member(message.from_user.id)
    if not member.status in ["administrator", "creator"]:
        return

    if not message.reply_to_message:
        return

    target = message.reply_to_message.from_user
    try:
        await bot.restrict_chat_member(
            chat_id=message.chat.id,
            user_id=target.id,
            permissions=ChatPermissions(can_send_messages=False),
            until_date=datetime.now() + timedelta(minutes=15)
        )
        await message.reply(f"🔇 {target.mention_html()} замучен на 15 минут.")
    except Exception as e:
        await message.reply(f"Ошибка: {e}")

@dp.message(Command("unmute"))
async def cmd_unmute(message: types.Message):
    member = await message.chat.get_member(message.from_user.id)
    if not member.status in ["administrator", "creator"]:
        return

    if not message.reply_to_message:
        return

    target = message.reply_to_message.from_user
    try:
        await bot.restrict_chat_member(
            chat_id=message.chat.id,
            user_id=target.id,
            permissions=ChatPermissions(
                can_send_messages=True,
                can_send_media_messages=True,
                can_send_other_messages=True,
                can_add_web_page_previews=True
            )
        )
        await message.reply(f"🔊 {target.mention_html()} размучен.")
    except Exception as e:
        await message.reply(f"Ошибка: {e}")

# --- АВТОМАТИЧЕСКИЕ ДЕЙСТВИЯ (Приветствие) ---

@dp.message(F.new_chat_members)
async def greet_new_member(message: types.Message):
    for new_member in message.new_chat_members:
        if new_member.is_bot:
            continue
        # Создаем запись в БД
        get_user(new_member.id, message.chat.id)
        await message.answer(f"👋 Привет, {new_member.mention_html()}! Добро пожаловать в чат.\n"
                             f"Не забудь прочитать правила и получить бонус командой /bonus")

# --- ЗАПУСК ---

async def main():
    init_db()
    print("Бот запущен...")
    # Удаляем вебхуки, чтобы не мешали polling
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        print("Бот выключен.")
