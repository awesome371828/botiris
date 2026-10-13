
Pear CM — однофайловая основа Telegram chat-manager.
Python 3.11+
Установка: pip install aiogram aiosqlite
Запуск: TELEGRAM_BOT_TOKEN=... python iris.py

Токен хранится только в переменной окружения. Не публикуйте его в Git.
Бот может модерировать только при наличии соответствующих прав администратора.
"""

import asyncio
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Optional

import aiosqlite
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatType, ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    CallbackQuery,
    ChatMemberUpdated,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
DB_PATH = os.getenv("PEAR_DB_PATH", "pear_cm.sqlite3")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("pear_cm")

dp = Dispatcher()
router = Router()
dp.include_router(router)


def main_menu() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="🛡 Модерация", callback_data="help:mod")
    kb.button(text="📊 Профиль", callback_data="help:profile")
    kb.button(text="💰 Экономика", callback_data="help:economy")
    kb.button(text="📜 Правила", callback_data="help:rules")
    kb.button(text="⚙️ Настройки чата", callback_data="help:settings")
    kb.adjust(2, 2, 1)
    return kb.as_markup()


async def init_db() -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript("""
        PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS chat_settings (
            chat_id INTEGER PRIMARY KEY,
            rules TEXT NOT NULL DEFAULT 'Правила пока не настроены.',
            welcome TEXT NOT NULL DEFAULT 'Добро пожаловать, {name}!'
        );
        CREATE TABLE IF NOT EXISTS warnings (
            chat_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            count INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY(chat_id, user_id)
        );
        CREATE TABLE IF NOT EXISTS profiles (
            user_id INTEGER PRIMARY KEY,
            coins INTEGER NOT NULL DEFAULT 0,
            messages INTEGER NOT NULL DEFAULT 0
        );
        """)
        await db.commit()


async def ensure_chat(chat_id: int) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT OR IGNORE INTO chat_settings(chat_id) VALUES(?)", (chat_id,))
        await db.commit()


async def get_rules(chat_id: int) -> str:
    await ensure_chat(chat_id)
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT rules FROM chat_settings WHERE chat_id=?", (chat_id,))
        row = await cur.fetchone()
        return row[0] if row else "Правила пока не настроены."


async def is_admin(bot: Bot, chat_id: int, user_id: int) -> bool:
    try:
        member = await bot.get_chat_member(chat_id, user_id)
        return member.status in ("creator", "administrator")
    except (TelegramBadRequest, TelegramForbiddenError):
        return False


async def require_group(message: Message) -> bool:
    return bool(message.chat.type in (ChatType.GROUP, ChatType.SUPERGROUP))


async def require_admin(message: Message, bot: Bot) -> bool:
    if not await require_group(message):
        await message.answer("Эта команда работает в группах.")
        return False
    if not message.from_user or not await is_admin(bot, message.chat.id, message.from_user.id):
        await message.answer("⛔ Команда доступна только администраторам чата.")
        return False
    return True


def target_from_reply(message: Message):
    if message.reply_to_message and message.reply_to_message.from_user:
        return message.reply_to_message.from_user
    return None


def parse_duration(value: str) -> Optional[timedelta]:
    """Поддерживает 30s, 15m, 2h, 3d."""
    match = re.fullmatch(r"(\d+)(s|m|h|d)", value.lower())
    if not match:
        return None
    amount = int(match.group(1))
    unit = match.group(2)
    if amount <= 0:
        return None
    return {
        "s": timedelta(seconds=amount),
        "m": timedelta(minutes=amount),
        "h": timedelta(hours=amount),
        "d": timedelta(days=amount),
    }[unit]


@router.message(CommandStart())
async def start(message: Message):
    await message.answer(
        "👋 <b>Pear CM</b>\n"
        "Твой чат-менеджер: модерация, правила, предупреждения и базовая экономика.\n\n"
        "Добавь меня в группу и выдай права администратора для модерации.",
        reply_markup=main_menu(),
    )


@router.message(Command("help"))
async def help_command(message: Message):
    await message.answer(
        "<b>Команды Pear CM</b>\n\n"
        "<b>Модерация (ответом на сообщение):</b>\n"
        "/ban — заблокировать участника\n"
        "/unban USER_ID — снять блокировку\n"
        "/kick — удалить участника из группы\n"
        "/mute 10m — ограничить сообщения на время\n"
        "/unmute — снять ограничения\n"
        "/warn — выдать предупреждение\n"
        "/warns — число предупреждений\n"
        "/clear 10 — удалить до 10 последних сообщений (команда и ответ не считаются гарантированно)\n\n"
        "<b>Информация:</b>\n"
        "/id — ID чата и пользователя\n"
        "/rules — правила чата\n"
        "/setrules текст — задать правила (админ)\n"
        "/setwelcome текст — задать приветствие; используй {name} (админ)\n"
        "/profile — профиль и сообщения\n"
        "/daily — получить ежедневные монеты\n"
        "/balance — баланс монет\n"
        "/menu — меню кнопок",
        reply_markup=main_menu(),
    )


@router.message(Command("menu"))
async def menu(message: Message):
    await message.answer("Выбери раздел:", reply_markup=main_menu())


@router.callback_query(F.data.startswith("help:"))
async def menu_callback(callback: CallbackQuery):
    section = callback.data.split(":", 1)[1]
    texts = {
        "mod": "🛡 <b>Модерация</b>\nОтветь на сообщение участника командой /ban, /kick, /mute 10m, /unmute или /warn. Для работы выдайте боту нужные права.",
        "profile": "📊 <b>Профиль</b>\nИспользуй /profile, чтобы посмотреть количество сообщений и баланс.",
        "economy": "💰 <b>Экономика</b>\nКоманды: /balance и /daily. Это базовая экономика, её можно расширять.",
        "rules": "📜 <b>Правила</b>\nКоманда /rules показывает правила текущего чата. Администратор задаёт их через /setrules текст.",
        "settings": "⚙️ <b>Настройки</b>\nАдминистратор может задать правила командой /setrules и приветствие командой /setwelcome.",
    }
    await callback.answer()
    if callback.message:
        await callback.message.edit_text(texts.get(section, "Раздел не найден."), reply_markup=main_menu())


@router.message(Command("id"))
async def id_command(message: Message):
    target = target_from_reply(message) or message.from_user
    await message.answer(
        f"Чат ID: <code>{message.chat.id}</code>\n"
        f"Пользователь ID: <code>{target.id if target else 'неизвестно'}</code>"
    )


@router.message(Command("rules"))
async def rules_command(message: Message):
    await message.answer("📜 <b>Правила чата</b>\n\n" + await get_rules(message.chat.id))


@router.message(Command("setrules"))
async def set_rules(message: Message, bot: Bot):
    if not await require_admin(message, bot):
        return
    text = (message.text or "").partition(" ")[2].strip()
    if not text:
        await message.answer("Укажи текст: <code>/setrules Уважайте друг друга</code>")
        return
    await ensure_chat(message.chat.id)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE chat_settings SET rules=? WHERE chat_id=?", (text[:4000], message.chat.id))
        await db.commit()
    await message.answer("✅ Правила чата сохранены.")


@router.message(Command("setwelcome"))
async def set_welcome(message: Message, bot: Bot):
    if not await require_admin(message, bot):
        return
    text = (message.text or "").partition(" ")[2].strip()
    if not text:
        await message.answer("Укажи приветствие. Можно использовать {name}.")
        return
    await ensure_chat(message.chat.id)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE chat_settings SET welcome=? WHERE chat_id=?", (text[:1000], message.chat.id))
        await db.commit()
    await message.answer("✅ Приветствие сохранено.")


@router.chat_member()
async def welcome_new_member(event: ChatMemberUpdated, bot: Bot):
    old = event.old_chat_member.status
    new = event.new_chat_member.status
    if old in ("left", "kicked") and new in ("member", "restricted"):
        user = event.new_chat_member.user
        await ensure_chat(event.chat.id)
        async with aiosqlite.connect(DB_PATH) as db:
            cur = await db.execute("SELECT welcome FROM chat_settings WHERE chat_id=?", (event.chat.id,))
            row = await cur.fetchone()
        welcome = row[0] if row else "Добро пожаловать, {name}!"
        await bot.send_message(event.chat.id, welcome.replace("{name}", user.full_name))


@router.message(Command("ban"))
async def ban_command(message: Message, bot: Bot):
    if not await require_admin(message, bot):
        return
    target = target_from_reply(message)
    if not target:
        await message.answer("Ответь командой /ban на сообщение участника.")
        return
    if target.id == message.from_user.id:
        await message.answer("Нельзя заблокировать самого себя этой командой.")
        return
    try:
        await bot.ban_chat_member(message.chat.id, target.id)
        await message.answer(f"🔨 Пользователь <a href='tg://user?id={target.id}'>{target.full_name}</a> заблокирован.")
    except TelegramBadRequest as e:
        await message.answer(f"Не удалось заблокировать: {e.message}")


@router.message(Command("unban"))
async def unban_command(message: Message, bot: Bot):
    if not await require_admin(message, bot):
        return
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) != 2 or not parts[1].isdigit():
        await message.answer("Формат: /unban USER_ID")
        return
    try:
        await bot.unban_chat_member(message.chat.id, int(parts[1]), only_if_banned=True)
        await message.answer("✅ Блокировка снята.")
    except TelegramBadRequest as e:
        await message.answer(f"Не удалось снять блокировку: {e.message}")


@router.message(Command("kick"))
async def kick_command(message: Message, bot: Bot):
    if not await require_admin(message, bot):
        return
    target = target_from_reply(message)
    if not target:
        await message.answer("Ответь командой /kick на сообщение участника.")
        return
    try:
        await bot.ban_chat_member(message.chat.id, target.id)
        await bot.unban_chat_member(message.chat.id, target.id, only_if_banned=True)
        await message.answer(f"👢 {target.full_name} удалён из группы.")
    except TelegramBadRequest as e:
        await message.answer(f"Не удалось удалить участника: {e.message}")


@router.message(Command("mute"))
async def mute_command(message: Message, bot: Bot):
    if not await require_admin(message, bot):
        return
    target = target_from_reply(message)
    parts = (message.text or "").split(maxsplit=1)
    duration = parse_duration(parts[1]) if len(parts) > 1 else None
    if not target or not duration:
        await message.answer("Ответь на сообщение: <code>/mute 10m</code> (например 30s, 15m, 2h, 3d).")
        return
    until = datetime.now(timezone.utc) + duration
    try:
        from aiogram.types import ChatPermissions
        await bot.restrict_chat_member(
            message.chat.id, target.id,
            permissions=ChatPermissions(can_send_messages=False),
            until_date=until,
        )
        await message.answer(f"🔇 {target.full_name} ограничен до {until.strftime('%H:%M:%S UTC')}.")
    except TelegramBadRequest as e:
        await message.answer(f"Не удалось ограничить: {e.message}")


@router.message(Command("unmute"))
async def unmute_command(message: Message, bot: Bot):
    if not await require_admin(message, bot):
        return
    target = target_from_reply(message)
    if not target:
        await message.answer("Ответь командой /unmute на сообщение участника.")
        return
    try:
        from aiogram.types import ChatPermissions
        await bot.restrict_chat_member(
            message.chat.id, target.id,
            permissions=ChatPermissions(
                can_send_messages=True,
                can_send_audios=True,
                can_send_documents=True,
                can_send_photos=True,
                can_send_videos=True,
                can_send_video_notes=True,
                can_send_voice_notes=True,
                can_send_polls=True,
                can_send_other_messages=True,
                can_add_web_page_previews=True,
            ),
        )
        await message.answer(f"🔊 Ограничения сняты для {target.full_name}.")
    except TelegramBadRequest as e:
        await message.answer(f"Не удалось снять ограничения: {e.message}")


@router.message(Command("warn"))
async def warn_command(message: Message, bot: Bot):
    if not await require_admin(message, bot):
        return
    target = target_from_reply(message)
    if not target:
        await message.answer("Ответь командой /warn на сообщение участника.")
        return
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO warnings(chat_id,user_id,count) VALUES(?,?,1) "
            "ON CONFLICT(chat_id,user_id) DO UPDATE SET count=count+1",
            (message.chat.id, target.id),
        )
        cur = await db.execute("SELECT count FROM warnings WHERE chat_id=? AND user_id=?", (message.chat.id, target.id))
        row = await cur.fetchone()
        await db.commit()
    count = row[0] if row else 1
    await message.answer(f"⚠️ {target.full_name}: предупреждение {count}.")


@router.message(Command("warns"))
async def warns_command(message: Message):
    target = target_from_reply(message) or message.from_user
    if not target:
        return
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT count FROM warnings WHERE chat_id=? AND user_id=?", (message.chat.id, target.id))
        row = await cur.fetchone()
    await message.answer(f"⚠️ Предупреждений: {row[0] if row else 0}")


@router.message(Command("clear"))
async def clear_command(message: Message, bot: Bot):
    if not await require_admin(message, bot):
        return
    parts = (message.text or "").split(maxsplit=1)
    try:
        count = int(parts[1]) if len(parts) > 1 else 10
    except ValueError:
        count = 10
    count = max(1, min(count, 50))
    deleted = 0
    # Telegram не предоставляет API для получения истории сообщений.
    # Поэтому очищаем только команду и сообщение, на которое ответили, если оно есть.
    try:
        await message.delete()
        deleted += 1
    except TelegramBadRequest:
        pass
    if message.reply_to_message:
        try:
            await message.reply_to_message.delete()
            deleted += 1
        except TelegramBadRequest:
            pass
    await message.answer(
        f"Удалено доступных сообщений: {deleted}. Telegram Bot API не позволяет боту "
        f"самостоятельно получить последние {count} сообщений из истории."
    )


@router.message(Command("balance"))
async def balance_command(message: Message):
    user = message.from_user
    if not user:
        return
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT OR IGNORE INTO profiles(user_id) VALUES(?)", (user.id,))
        cur = await db.execute("SELECT coins FROM profiles WHERE user_id=?", (user.id,))
        row = await cur.fetchone()
        await db.commit()
    await message.answer(f"💰 Баланс: <b>{row[0] if row else 0}</b> монет.")


@router.message(Command("daily"))
async def daily_command(message: Message):
    user = message.from_user
    if not user:
        return
    # Базовая версия: награда при каждом вызове. Для production добавьте таблицу daily_claims
    # с датой последнего получения, чтобы ограничить выдачу одним разом в сутки.
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT OR IGNORE INTO profiles(user_id) VALUES(?)", (user.id,))
        await db.execute("UPDATE profiles SET coins=coins+10 WHERE user_id=?", (user.id,))
        cur = await db.execute("SELECT coins FROM profiles WHERE user_id=?", (user.id,))
        row = await cur.fetchone()
        await db.commit()
    await message.answer(f"🎁 Начислено 10 монет! Баланс: <b>{row[0]}</b>.")


@router.message(Command("profile"))
async def profile_command(message: Message):
    user = message.from_user
    if not user:
        return
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT OR IGNORE INTO profiles(user_id) VALUES(?)", (user.id,))
        cur = await db.execute("SELECT coins,messages FROM profiles WHERE user_id=?", (user.id,))
        row = await cur.fetchone()
        await db.commit()
    await message.answer(
        f"👤 <b>{user.full_name}</b>\n"
        f"ID: <code>{user.id}</code>\n"
        f"Монеты: <b>{row[0]}</b>\n"
        f"Сообщения: <b>{row[1]}</b>"
    )


@router.message()
async def count_messages(message: Message):
    if not message.from_user or message.from_user.is_bot:
        return
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT OR IGNORE INTO profiles(user_id) VALUES(?)", (message.from_user.id,))
        await db.execute("UPDATE profiles SET messages=messages+1 WHERE user_id=?", (message.from_user.id,))
        await db.commit()


async def main():
    if not TOKEN:
        raise RuntimeError(
            "Не задан TELEGRAM_BOT_TOKEN. Создай секрет в GitHub или переменную окружения локально."
        )
    await init_db()
    bot = Bot(TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    me = await bot.get_me()
    log.info("Pear CM запущен: @%s (id=%s)", me.username, me.id)
    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
