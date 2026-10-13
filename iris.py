"""
Pear / Iris-style Telegram group manager — independent implementation.
Python 3.11+, aiogram 3, Supabase.
Set TELEGRAM_BOT_TOKEN, SUPABASE_URL and SUPABASE_SERVICE_KEY in environment.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Any

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatMemberStatus, ChatType, ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    BotCommand, ChatPermissions, KeyboardButton, Message,
    ReplyKeyboardMarkup, ReplyKeyboardRemove,
)
from supabase import Client, create_client

# ── Configuration ────────────────────────────────────────────────────────────
OWNER_ID = int(os.getenv("BOT_OWNER_ID", "6652898792"))
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
SUPABASE_URL = os.getenv("SUPABASE_URL", "").strip()
SUPABASE_KEY = (
    os.getenv("SUPABASE_SERVICE_KEY", "").strip()
    or os.getenv("SUPABASE_ANON", "").strip()
    or os.getenv("SUPABASE_PUBL", "").strip()
)
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("pear_cm")

if not BOT_TOKEN:
    raise RuntimeError("Не задан TELEGRAM_BOT_TOKEN в Secrets / переменных окружения.")

db: Client | None = None
if SUPABASE_URL and SUPABASE_KEY:
    try:
        db = create_client(SUPABASE_URL, SUPABASE_KEY)
        log.info("Supabase client configured")
    except Exception:
        log.exception("Не удалось создать клиент Supabase")
else:
    log.warning("Supabase не настроен: данные будут храниться только в рамках процесса.")

bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()
router = Router()
dp.include_router(router)

# ── Keyboards ────────────────────────────────────────────────────────────────
def main_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🔀 Случайный чат")],
            [KeyboardButton(text="🏅 Чаты: топ дня"), KeyboardButton(text="🏆 Супер топ чатов")],
            [KeyboardButton(text="💠 Профиль"), KeyboardButton(text="💍 Брак")],
            [KeyboardButton(text="🛠 Установка"), KeyboardButton(text="🔗 Команды")],
            [KeyboardButton(text="⚠️ Мои варны"), KeyboardButton(text="🛡 Кто админ")],
        ],
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder="Выберите раздел или введите команду…",
    )

def mention(user_id: int, name: str) -> str:
    safe = (name or "пользователь").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return f'<a href="tg://user?id={user_id}">{safe}</a>'

# ── Database helpers ─────────────────────────────────────────────────────────
def db_one(table: str, **filters: Any) -> dict | None:
    if db is None:
        return None
    try:
        q = db.table(table).select("*")
        for key, value in filters.items():
            q = q.eq(key, value)
        rows = q.limit(1).execute().data or []
        return rows[0] if rows else None
    except Exception:
        log.exception("Supabase select failed: %s", table)
        return None

def db_rows(table: str, limit: int = 20, order: str | None = None, **filters: Any) -> list[dict]:
    if db is None:
        return []
    try:
        q = db.table(table).select("*")
        for key, value in filters.items():
            q = q.eq(key, value)
        if order:
            q = q.order(order, desc=True)
        return q.limit(limit).execute().data or []
    except Exception:
        log.exception("Supabase query failed: %s", table)
        return []

def db_insert(table: str, payload: dict) -> dict | None:
    if db is None:
        return None
    try:
        result = db.table(table).insert(payload).execute()
        return (result.data or [None])[0]
    except Exception:
        log.exception("Supabase insert failed: %s", table)
        return None

def db_upsert(table: str, payload: dict, on_conflict: str) -> dict | None:
    if db is None:
        return None
    try:
        result = db.table(table).upsert(payload, on_conflict=on_conflict).execute()
        return (result.data or [None])[0]
    except Exception:
        log.exception("Supabase upsert failed: %s", table)
        return None

def ensure_user(user_id: int, chat_id: int, username: str | None, first_name: str) -> dict:
    current = db_one("bot_users", user_id=user_id, chat_id=chat_id)
    if current:
        db_upsert("bot_users", {
            "user_id": user_id, "chat_id": chat_id, "username": username,
            "first_name": first_name or "Пользователь",
            "last_seen": datetime.now(timezone.utc).isoformat(),
        }, "user_id,chat_id")
        current.update({"username": username, "first_name": first_name or "Пользователь"})
        return current
    row = db_upsert("bot_users", {
        "user_id": user_id, "chat_id": chat_id, "username": username,
        "first_name": first_name or "Пользователь", "warns": 0, "rank": 0,
        "messages": 0, "last_seen": datetime.now(timezone.utc).isoformat(),
    }, "user_id,chat_id")
    return row or {"user_id": user_id, "chat_id": chat_id, "warns": 0, "rank": 0, "messages": 0}

def audit(chat_id: int, actor_id: int, action: str, target_id: int | None = None, details: str = "") -> None:
    db_insert("bot_audit", {
        "chat_id": chat_id, "actor_id": actor_id, "action": action,
        "target_id": target_id, "details": details[:1000],
    })

def set_warns(chat_id: int, user_id: int, amount: int) -> int:
    user = db_one("bot_users", user_id=user_id, chat_id=chat_id) or {
        "user_id": user_id, "chat_id": chat_id, "username": None,
        "first_name": "Пользователь", "rank": 0, "messages": 0,
    }
    new_count = max(0, int(user.get("warns", 0)) + amount)
    db_upsert("bot_users", {**user, "user_id": user_id, "chat_id": chat_id, "warns": new_count}, "user_id,chat_id")
    return new_count

# ── Shared helpers ───────────────────────────────────────────────────────────
def is_group(message: Message) -> bool:
    return message.chat.type in (ChatType.GROUP, ChatType.SUPERGROUP)

async def is_owner(user_id: int) -> bool:
    return user_id == OWNER_ID

async def is_admin(bot: Bot, chat_id: int, user_id: int) -> bool:
    if user_id == OWNER_ID:
        return True
    try:
        member = await bot.get_chat_member(chat_id, user_id)
        return member.status in (ChatMemberStatus.CREATOR, ChatMemberStatus.ADMINISTRATOR)
    except (TelegramBadRequest, TelegramForbiddenError):
        return False

async def require_admin(message: Message) -> bool:
    if not message.from_user or not is_group(message):
        await message.answer("Эта команда работает в группе.")
        return False
    if not await is_admin(bot, message.chat.id, message.from_user.id):
        await message.answer("⛔ Команда доступна только администраторам чата.")
        return False
    return True

def parse_duration(value: str) -> timedelta | None:
    """Accept 30с, 10м, 2ч, 3д, 1н, plus common English units."""
    match = re.fullmatch(r"\s*(\d{1,5})\s*(с|сек|секунд[аы]?|s|м|мин|минут[аы]?|m|ч|час(?:а|ов)?|h|д|дн|день|дня|days?|d|н|нед(?:еля|ели|ель)?|w)\s*", value.lower())
    if not match:
        return None
    number = int(match.group(1))
    unit = match.group(2)
    if number < 1:
        return None
    if unit in {"с", "сек", "секунда", "секунды", "секунд", "s"}:
        seconds = number
    elif unit in {"м", "мин", "минута", "минуты", "минут", "m"}:
        seconds = number * 60
    elif unit in {"ч", "час", "часа", "часов", "h"}:
        seconds = number * 3600
    elif unit in {"д", "дн", "день", "дня", "day", "days", "d"}:
        seconds = number * 86400
    else:
        seconds = number * 604800
    if seconds > 366 * 86400:
        return None
    return timedelta(seconds=seconds)

async def resolve_target(message: Message, args: list[str]) -> tuple[int, str] | None:
    if message.reply_to_message and message.reply_to_message.from_user:
        u = message.reply_to_message.from_user
        return u.id, u.full_name
    if args:
        raw = args[0].strip()
        if raw.isdigit():
            return int(raw), f"ID {raw}"
        if raw.startswith("@"):
            username = raw[1:].lower()
            # Telegram doesn't offer a general username-to-ID lookup; use users known to this chat.
            rows = db_rows("bot_users", limit=1000, chat_id=message.chat.id)
            for row in rows:
                if (row.get("username") or "").lower() == username:
                    return int(row["user_id"]), row.get("first_name") or raw
    return None

def args_after_command(message: Message) -> list[str]:
    parts = (message.text or "").split()
    return parts[1:] if len(parts) > 1 else []

async def bot_can_restrict(message: Message) -> bool:
    try:
        me = await bot.get_me()
        member = await bot.get_chat_member(message.chat.id, me.id)
        return member.status in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR)
    except Exception:
        return False

async def safe_error(message: Message, exc: Exception) -> None:
    log.exception("Command failed", exc_info=exc)
    await message.answer("⚠️ Telegram не выполнил действие. Проверьте права бота и цель команды.")

# ── Welcome and menu ─────────────────────────────────────────────────────────
@router.message(CommandStart())
async def start(message: Message) -> None:
    if message.chat.type == ChatType.PRIVATE:
        text = (
            "👋 <b>Привет! Я Pear CM</b> — бот для управления группой.\n\n"
            "Добавьте меня в группу и выдайте права администратора, если нужны "
            "муты, баны, удаление сообщений и закрепление.\n\n"
            "Откройте <b>🔗 Команды</b>, чтобы посмотреть возможности."
        )
        await message.answer(text, reply_markup=main_keyboard())
    else:
        await message.answer(
            f"👋 Бот включён в чате <b>{message.chat.title or 'группа'}</b>!\n"
            "Команды: /help, /commands, /profile, /admins."
        )

@router.message(Command("help", "commands"))
@router.message(F.text.in_({"🔗 Команды", "❓ Помощь"}))
async def help_command(message: Message) -> None:
    await message.answer(
        "<b>📚 Команды Pear CM</b>\n\n"
        "<b>Профиль и чат</b>\n"
        "• /profile — профиль; /top — активные участники\n"
        "• /admins или «🛡 Кто админ» — список админов\n"
        "• /warns — мои предупреждения; /marry — брак в ответ на сообщение\n\n"
        "<b>Модерация (ответьте на сообщение участника)</b>\n"
        "• /mute 10м или «мут 10м» — ограничить сообщения\n"
        "• /unmute — снять мут; /ban — бан; /unban ID — разбан\n"
        "• /kick — исключить; /warn — выдать предупреждение\n"
        "• /unwarn — снять одно предупреждение; /clearwarns — обнулить\n"
        "• /del или «-смс» — удалить сообщение; /purge 10 — очистить сообщения\n"
        "• /pin или «.пин» — закрепить сообщение; /unpin или «.анпин» — открепить\n"
        "• /info — информация об участнике; /logs — последние действия\n\n"
        "<b>Важно:</b> команды модерации требуют прав администратора. "
        "Для mute/ban/delete/pin сам бот тоже должен иметь соответствующие права.",
        reply_markup=main_keyboard(),
    )

@router.message(F.text.in_({"📋 Меню", "Меню", "🛠 Установка"}))
async def menu_button(message: Message) -> None:
    await message.answer(
        "👋 <b>Главное меню</b>\n\n"
        "Выберите раздел ниже или используйте /commands.\n"
        "Для работы в группе добавьте бота и выдайте необходимые права.",
        reply_markup=main_keyboard(),
    )

@router.message(F.text == "🔀 Случайный чат")
async def random_chat(message: Message) -> None:
    await message.answer("🔀 Эта функция требует отдельного сервиса знакомств/случайных чатов и здесь не включена.")

@router.message(F.text.in_({"🏅 Чаты: топ дня", "🏆 Супер топ чатов"}))
async def chat_top_placeholder(message: Message) -> None:
    await message.answer("🏆 Рейтинг чатов пока не собирается. В этой версии доступны профиль и топ участников группы.")

@router.message(F.text == "💠 Профиль")
@router.message(Command("profile"))
async def profile(message: Message) -> None:
    if not message.from_user:
        return
    chat_id = message.chat.id
    user = ensure_user(message.from_user.id, chat_id, message.from_user.username, message.from_user.full_name)
    await message.answer(
        f"👤 <b>Профиль</b>\n{mention(message.from_user.id, message.from_user.full_name)}\n\n"
        f"⚠️ Предупреждения: <b>{int(user.get('warns', 0))}</b>\n"
        f"🏅 Ранг: <b>{int(user.get('rank', 0))}</b>\n"
        f"💬 Сообщений: <b>{int(user.get('messages', 0))}</b>"
    )

@router.message(F.text.in_({"⚠️ Мои варны", "Варны"}))
@router.message(Command("warns"))
async def my_warns(message: Message) -> None:
    if not message.from_user:
        return
    user = ensure_user(message.from_user.id, message.chat.id, message.from_user.username, message.from_user.full_name)
    await message.answer(f"⚠️ У вас <b>{int(user.get('warns', 0))}</b> предупреждений.")

@router.message(F.text == "💍 Брак")
async def marriage_menu(message: Message) -> None:
    await message.answer("💍 Чтобы предложить брак, ответьте на сообщение человека командой <code>/marry</code>.\nПосмотреть статус: <code>/marriage</code>.")

@router.message(F.text == "🛡 Кто админ")
@router.message(Command("admins"))
async def admins(message: Message) -> None:
    try:
        members = await bot.get_chat_administrators(message.chat.id)
        lines = ["🛡 <b>Администраторы чата</b>"]
        for member in members[:50]:
            u = member.user
            label = "создатель" if member.status == ChatMemberStatus.CREATOR else "администратор"
            lines.append(f"• {mention(u.id, u.full_name)} — {label}")
        await message.answer("\n".join(lines))
    except Exception:
        await message.answer("Не удалось получить список. Проверьте, что бот добавлен в группу.")

# ── Activity tracking and welcome ────────────────────────────────────────────
@router.message(F.new_chat_members)
async def welcome_members(message: Message) -> None:
    for user in message.new_chat_members:
        if user.is_bot:
            continue
        ensure_user(user.id, message.chat.id, user.username, user.full_name)
        await message.answer(f"👋 Добро пожаловать, {mention(user.id, user.full_name)}!")

@router.message(F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}), F.from_user)
async def track_activity(message: Message) -> None:
    # This handler is registered last and only records regular messages.
    if not message.from_user or message.from_user.is_bot or not message.text:
        return
    user = ensure_user(message.from_user.id, message.chat.id, message.from_user.username, message.from_user.full_name)
    db_upsert("bot_users", {
        "user_id": message.from_user.id, "chat_id": message.chat.id,
        "username": message.from_user.username, "first_name": message.from_user.full_name,
        "warns": int(user.get("warns", 0)), "rank": int(user.get("rank", 0)),
        "messages": int(user.get("messages", 0)) + 1,
        "last_seen": datetime.now(timezone.utc).isoformat(),
    }, "user_id,chat_id")

# ── Moderation: shared command implementation ─────────────────────────────────
async def moderation_target(message: Message, command: str) -> tuple[int, str] | None:
    target = await resolve_target(message, args_after_command(message))
    if not target:
        await message.answer(f"Ответьте на сообщение участника командой <code>/{command}</code>.")
    elif message.from_user and target[0] == message.from_user.id:
        await message.answer("Нельзя применить это действие к самому себе.")
        return None
    elif target[0] == OWNER_ID and message.from_user and message.from_user.id != OWNER_ID:
        await message.answer("⛔ Нельзя применять это действие к владельцу бота.")
        return None
    return target

async def check_mod(message: Message) -> bool:
    if not await require_admin(message):
        return False
    if not await bot_can_restrict(message):
        await message.answer("⚠️ Сначала выдайте боту права администратора с нужными разрешениями.")
        return False
    return True

@router.message(Command("mute"))
@router.message(F.text.regexp(r"(?i)^мут(?:\s|$)"))
async def mute_cmd(message: Message) -> None:
    if not await check_mod(message):
        return
    args = args_after_command(message) if (message.text or "").startswith("/") else (message.text or "").split()[1:]
    duration = parse_duration(args[-1]) if args else None
    if not duration:
        await message.answer("Формат: ответьте на сообщение и напишите <code>/mute 10м</code> (с, м, ч, д, н).")
        return
    target = await moderation_target(message, "mute")
    if not target:
        return
    until = datetime.now(timezone.utc) + duration
    try:
        await bot.restrict_chat_member(
            message.chat.id, target[0],
            permissions=ChatPermissions(can_send_messages=False),
            until_date=until,
        )
        audit(message.chat.id, message.from_user.id, "mute", target[0], f"until={until.isoformat()}")
        await message.answer(f"🔇 {mention(target[0], target[1])} ограничен до <b>{until.strftime('%Y-%m-%d %H:%M UTC')}</b>.")
    except Exception as exc:
        await safe_error(message, exc)

@router.message(Command("unmute"))
@router.message(F.text.regexp(r"(?i)^размут(?:\s|$)"))
async def unmute_cmd(message: Message) -> None:
    if not await check_mod(message):
        return
    target = await moderation_target(message, "unmute")
    if not target:
        return
    try:
        await bot.restrict_chat_member(
            message.chat.id, target[0],
            permissions=ChatPermissions(
                can_send_messages=True, can_send_audios=True, can_send_documents=True,
                can_send_photos=True, can_send_videos=True, can_send_video_notes=True,
                can_send_voice_notes=True, can_send_polls=True, can_send_other_messages=True,
                can_add_web_page_previews=True,
            ),
        )
        audit(message.chat.id, message.from_user.id, "unmute", target[0])
        await message.answer(f"🔊 Ограничения сняты с {mention(target[0], target[1])}.")
    except Exception as exc:
        await safe_error(message, exc)

@router.message(Command("ban"))
@router.message(F.text.regexp(r"(?i)^бан(?:\s|$)"))
async def ban_cmd(message: Message) -> None:
    if not await check_mod(message):
        return
    target = await moderation_target(message, "ban")
    if not target:
        return
    try:
        await bot.ban_chat_member(message.chat.id, target[0])
        audit(message.chat.id, message.from_user.id, "ban", target[0])
        await message.answer(f"⛔ {mention(target[0], target[1])} заблокирован в чате.")
    except Exception as exc:
        await safe_error(message, exc)

@router.message(Command("unban"))
async def unban_cmd(message: Message) -> None:
    if not await check_mod(message):
        return
    args = args_after_command(message)
    if not args or not args[0].isdigit():
        await message.answer("Формат: <code>/unban ID_пользователя</code>.")
        return
    try:
        await bot.unban_chat_member(message.chat.id, int(args[0]), only_if_banned=True)
        audit(message.chat.id, message.from_user.id, "unban", int(args[0]))
        await message.answer("✅ Пользователь разблокирован.")
    except Exception as exc:
        await safe_error(message, exc)

@router.message(Command("kick"))
@router.message(F.text.regexp(r"(?i)^кик(?:\s|$)"))
async def kick_cmd(message: Message) -> None:
    if not await check_mod(message):
        return
    target = await moderation_target(message, "kick")
    if not target:
        return
    try:
        await bot.ban_chat_member(message.chat.id, target[0])
        await bot.unban_chat_member(message.chat.id, target[0], only_if_banned=True)
        audit(message.chat.id, message.from_user.id, "kick", target[0])
        await message.answer(f"👢 {mention(target[0], target[1])} исключён из чата.")
    except Exception as exc:
        await safe_error(message, exc)

@router.message(Command("warn"))
@router.message(F.text.regexp(r"(?i)^варн(?:\s|$)"))
async def warn_cmd(message: Message) -> None:
    if not await require_admin(message):
        return
    target = await moderation_target(message, "warn")
    if not target:
        return
    count = set_warns(message.chat.id, target[0], 1)
    audit(message.chat.id, message.from_user.id, "warn", target[0], f"warns={count}")
    await message.answer(f"⚠️ {mention(target[0], target[1])}: предупреждение <b>{count}</b>.")
    if count >= 3 and await bot_can_restrict(message):
        try:
            until = datetime.now(timezone.utc) + timedelta(hours=1)
            await bot.restrict_chat_member(
                message.chat.id, target[0],
                permissions=ChatPermissions(can_send_messages=False), until_date=until,
            )
            audit(message.chat.id, message.from_user.id, "auto_mute", target[0], "3 warns")
            await message.answer("🔇 Автоматический мут на 1 час за 3 предупреждения.")
        except Exception:
            log.exception("Auto-mute failed")

@router.message(Command("unwarn"))
async def unwarn_cmd(message: Message) -> None:
    if not await require_admin(message):
        return
    target = await moderation_target(message, "unwarn")
    if not target:
        return
    count = set_warns(message.chat.id, target[0], -1)
    audit(message.chat.id, message.from_user.id, "unwarn", target[0], f"warns={count}")
    await message.answer(f"✅ Предупреждений осталось: <b>{count}</b>.")

@router.message(Command("clearwarns"))
async def clearwarns_cmd(message: Message) -> None:
    if not await require_admin(message):
        return
    target = await moderation_target(message, "clearwarns")
    if not target:
        return
    user = db_one("bot_users", user_id=target[0], chat_id=message.chat.id) or {}
    db_upsert("bot_users", {
        **user, "user_id": target[0], "chat_id": message.chat.id,
        "warns": 0, "rank": int(user.get("rank", 0)), "messages": int(user.get("messages", 0)),
    }, "user_id,chat_id")
    audit(message.chat.id, message.from_user.id, "clearwarns", target[0])
    await message.answer(f"🧹 Предупреждения сброшены для {mention(target[0], target[1])}.")

# ── Message tools ─────────────────────────────────────────────────────────────
@router.message(Command("del"))
@router.message(F.text == "-смс")
async def delete_message_cmd(message: Message) -> None:
    if not await require_admin(message):
        return
    target_message = message.reply_to_message
    if not target_message:
        await message.answer("Ответьте на сообщение командой <code>/del</code> или <code>-смс</code>.")
        return
    try:
        await target_message.delete()
        await message.delete()
        audit(message.chat.id, message.from_user.id, "delete", target_message.from_user.id if target_message.from_user else None)
    except Exception as exc:
        await safe_error(message, exc)

@router.message(Command("purge"))
async def purge_cmd(message: Message) -> None:
    if not await check_mod(message):
        return
    if not message.reply_to_message:
        await message.answer("Ответьте на самое старое сообщение и напишите <code>/purge 10</code>.")
        return
    args = args_after_command(message)
    try:
        count = max(1, min(int(args[0]) if args else 10, 100))
    except ValueError:
        count = 10
    deleted = 0
    start_id = message.reply_to_message.message_id
    for mid in range(start_id, min(start_id + count, message.message_id + 1)):
        try:
            await bot.delete_message(message.chat.id, mid)
            deleted += 1
        except TelegramBadRequest:
            continue
        except TelegramForbiddenError:
            break
    try:
        await message.delete()
    except Exception:
        pass
    audit(message.chat.id, message.from_user.id, "purge", details=f"deleted={deleted}")
    await message.answer(f"🧹 Удалено сообщений: <b>{deleted}</b>.")

@router.message(Command("pin"))
@router.message(F.text == ".пин")
async def pin_cmd(message: Message) -> None:
    if not await require_admin(message):
        return
    target_message = message.reply_to_message
    if not target_message:
        await message.answer("Ответьте на сообщение командой <code>/pin</code> или <code>.пин</code>.")
        return
    try:
        await target_message.pin()
        await message.answer("📌 Сообщение закреплено.")
        audit(message.chat.id, message.from_user.id, "pin", target_message.from_user.id if target_message.from_user else None)
    except Exception as exc:
        await safe_error(message, exc)

@router.message(Command("unpin"))
@router.message(F.text == ".анпин")
async def unpin_cmd(message: Message) -> None:
    if not await require_admin(message):
        return
    try:
        if message.reply_to_message:
            await message.reply_to_message.unpin()
        else:
            await bot.unpin_chat_message(message.chat.id)
        await message.answer("📌 Закрепление снято.")
        audit(message.chat.id, message.from_user.id, "unpin")
    except Exception as exc:
        await safe_error(message, exc)

@router.message(Command("info"))
async def info_cmd(message: Message) -> None:
    target = await resolve_target(message, args_after_command(message))
    if not target and message.from_user:
        target = (message.from_user.id, message.from_user.full_name)
    if not target:
        await message.answer("Ответьте на сообщение или укажите ID.")
        return
    user = db_one("bot_users", user_id=target[0], chat_id=message.chat.id) or {}
    try:
        member = await bot.get_chat_member(message.chat.id, target[0])
        status = str(member.status).replace("ChatMemberStatus.", "")
    except Exception:
        status = "не состоит / неизвестно"
    await message.answer(
        f"👤 <b>Информация</b>\nИмя: {mention(target[0], target[1])}\n"
        f"ID: <code>{target[0]}</code>\nСтатус: <code>{status}</code>\n"
        f"Варны: <b>{int(user.get('warns', 0))}</b>\n"
        f"Ранг: <b>{int(user.get('rank', 0))}</b>\n"
        f"Сообщений: <b>{int(user.get('messages', 0))}</b>"
    )

@router.message(Command("top"))
async def top_cmd(message: Message) -> None:
    rows = db_rows("bot_users", limit=10, order="messages", chat_id=message.chat.id)
    if not rows:
        await message.answer("Пока нет данных для топа. Сообщения начнут учитываться после настройки Supabase.")
        return
    lines = ["🏆 <b>Топ участников по сообщениям</b>"]
    for index, row in enumerate(rows, 1):
        lines.append(f"{index}. {mention(int(row['user_id']), row.get('first_name') or 'Участник')} — {int(row.get('messages', 0))} 💬")
    await message.answer("\n".join(lines))

@router.message(Command("logs"))
async def logs_cmd(message: Message) -> None:
    if not await require_admin(message):
        return
    rows = db_rows("bot_audit", limit=10, order="created_at", chat_id=message.chat.id)
    if not rows:
        await message.answer("📒 Логов пока нет.")
        return
    lines = ["📒 <b>Последние действия</b>"]
    for row in rows:
        stamp = (row.get("created_at") or "")[:16].replace("T", " ")
        lines.append(f"• <code>{stamp}</code> — {row.get('action', '?')} → <code>{row.get('target_id') or '—'}</code> {row.get('details') or ''}")
    await message.answer("\n".join(lines))

# ── Simple marriage feature ───────────────────────────────────────────────────
@router.message(Command("marry"))
async def marry_cmd(message: Message) -> None:
    if not message.from_user or not is_group(message):
        await message.answer("💍 Брак можно предложить в группе, ответив на сообщение участника командой /marry.")
        return
    if not message.reply_to_message or not message.reply_to_message.from_user:
        await message.answer("Ответьте на сообщение человека командой <code>/marry</code>.")
        return
    partner = message.reply_to_message.from_user
    if partner.id == message.from_user.id or partner.is_bot:
        await message.answer("С этим пользователем нельзя заключить брак.")
        return
    existing = db_one("bot_marriages", chat_id=message.chat.id, user1_id=message.from_user.id)
    reverse = db_one("bot_marriages", chat_id=message.chat.id, user1_id=partner.id)
    if existing or reverse:
        await message.answer("💍 Один из вас уже состоит в браке в этой группе.")
        return
    db_insert("bot_marriages", {
        "chat_id": message.chat.id, "user1_id": message.from_user.id,
        "user1_name": message.from_user.full_name, "user2_id": partner.id,
        "user2_name": partner.full_name, "status": "pending",
    })
    await message.answer(
        f"💍 {mention(message.from_user.id, message.from_user.full_name)} предлагает брак "
        f"{mention(partner.id, partner.full_name)}!\n"
        f"Партнёр может ответить командой <code>/acceptmarry</code> в ответ на это сообщение."
    )

@router.message(Command("acceptmarry"))
async def accept_marry_cmd(message: Message) -> None:
    if not message.from_user or not message.reply_to_message or not message.reply_to_message.from_user:
        await message.answer("Ответьте на сообщение с предложением брака командой /acceptmarry.")
        return
    proposer = message.reply_to_message.from_user
    row = db_one("bot_marriages", chat_id=message.chat.id, user1_id=proposer.id)
    if not row or int(row.get("user2_id", 0)) != message.from_user.id or row.get("status") != "pending":
        await message.answer("Активное предложение этому пользователю не найдено.")
        return
    if db is not None:
        try:
            db.table("bot_marriages").update({"status": "married", "accepted_at": datetime.now(timezone.utc).isoformat()}).eq("id", row["id"]).execute()
        except Exception:
            log.exception("Could not accept marriage")
            await message.answer("Не удалось сохранить брак в базе данных.")
            return
    await message.answer(f"💍 Поздравляем! {mention(proposer.id, proposer.full_name)} и {mention(message.from_user.id, message.from_user.full_name)} теперь в браке!")

@router.message(Command("marriage"))
async def marriage_status_cmd(message: Message) -> None:
    if not message.from_user:
        return
    rows = db_rows("bot_marriages", limit=100, chat_id=message.chat.id)
    for row in rows:
        if row.get("status") == "married" and message.from_user.id in (row.get("user1_id"), row.get("user2_id")):
            await message.answer(f"💍 Брак: <b>{row.get('user1_name', 'Участник')}</b> ❤️ <b>{row.get('user2_name', 'Участник')}</b>")
            return
    await message.answer("💔 Вы пока не состоите в браке в этой группе.")

@router.message(Command("divorce"))
async def divorce_cmd(message: Message) -> None:
    if not message.from_user:
        return
    rows = db_rows("bot_marriages", limit=100, chat_id=message.chat.id)
    for row in rows:
        if row.get("status") == "married" and message.from_user.id in (row.get("user1_id"), row.get("user2_id")):
            try:
                db.table("bot_marriages").update({"status": "divorced"}).eq("id", row["id"]).execute()
                await message.answer("💔 Брак расторгнут.")
            except Exception:
                await message.answer("Не удалось изменить запись в базе данных.")
            return
    await message.answer("У вас нет активного брака.")

# ── Global error handling / startup ──────────────────────────────────────────
@dp.errors()
async def global_error_handler(event) -> bool:
    log.error("Unhandled update error: %r", getattr(event, "exception", event))
    return True

async def main() -> None:
    await bot.set_my_commands([
        BotCommand(command="start", description="Открыть меню"),
        BotCommand(command="help", description="Справка и команды"),
        BotCommand(command="profile", description="Мой профиль"),
        BotCommand(command="admins", description="Администраторы чата"),
        BotCommand(command="warns", description="Мои предупреждения"),
        BotCommand(command="top", description="Топ участников"),
        BotCommand(command="mute", description="Мут участника (ответ + срок)"),
        BotCommand(command="unmute", description="Снять мут (ответ)"),
        BotCommand(command="ban", description="Забанить (ответ)"),
        BotCommand(command="kick", description="Исключить (ответ)"),
        BotCommand(command="warn", description="Выдать предупреждение (ответ)"),
        BotCommand(command="pin", description="Закрепить сообщение (ответ)"),
        BotCommand(command="del", description="Удалить сообщение (ответ)"),
        BotCommand(command="marry", description="Предложить брак (ответ)"),
    ])
    await bot.delete_webhook(drop_pending_updates=False)
    log.info("Bot started")
    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        await bot.session.close()

if __name__ == "__main__":
    asyncio.run(main())
