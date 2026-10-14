"""
Pear / Iris-style Telegram group manager — independent implementation.
Python 3.11+, aiogram 3, Supabase.
Set TELEGRAM_BOT_TOKEN, SUPABASE_URL and SUPABASE_SERVICE_KEY in environment.
"""
from __future__ import annotations

import asyncio
import ast
import json
import platform
import random
import sys
from io import BytesIO
import logging
import os
import re
import time
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from typing import Any

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatMemberStatus, ChatType, ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    BotCommand, BotCommandScopeAllGroupChats, BotCommandScopeAllPrivateChats,
    BotCommandScopeChat, ChatPermissions, KeyboardButton, Message, BufferedInputFile,
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
MOSCOW_TZ = ZoneInfo("Europe/Moscow")

def moscow_now() -> datetime:
    """Current aware Moscow time (MSK, UTC+3), updated from system time."""
    return datetime.now(MOSCOW_TZ)

def format_moscow_time(value: datetime) -> str:
    """Format an aware/UTC datetime in Moscow time."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(MOSCOW_TZ).strftime("%d.%m.%Y в %H:%M:%S МСК")

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

# Runtime cache is used if Supabase is temporarily unavailable. Settings are also
# persisted in bot_chat_settings when the table from schema.sql exists.
FILTER_DEFAULTS = {
    "antilink": False, "antiinvite": False, "antimat": False,
    "antiflood": False, "anticaps": False, "antireklama": False,
    "antikripta": False, "antifishing": False, "antiall": False,
    "antibot": False, "antisticker": False, "antigif": False, "antispam": False, "antiraid": False,
    "antiphoto": False, "antivideo": False, "antivoice": False,
    "antidocument": False, "flood_limit": 5, "flood_window": 10,
    "caps_percent": 70, "filter_mute_minutes": 10, "raid_threshold": 8, "raid_window": 30, "raid_restrict_minutes": 2,
    "warn_3_action": "mute", "warn_3_time": "1ч",
    "warn_5_action": "mute", "warn_5_time": "24ч",
    "warn_7_action": "ban", "warn_7_time": "7д",
    "warn_10_action": "ban", "warn_10_time": "навсегда",
}
_CHAT_SETTINGS_CACHE: dict[int, dict[str, Any]] = {}
_FLOOD_TIMES: dict[tuple[int, int], deque[float]] = defaultdict(deque)
_SPAM_LAST: dict[tuple[int, int], tuple[str, float]] = {}
_RAID_JOINS: dict[int, deque[float]] = defaultdict(deque)
_RAID_RESTRICTED: dict[tuple[int, int], float] = {}
BAD_WORDS = ("хуй", "пизд", "бляд", "ебал", "ебат", "сука", "мраз", "fuck", "shit", "bitch", "asshole")
AD_WORDS = ("купить", "продам", "продаю", "скидка", "акция", "распродажа", "заработок", "инвестиции", "крипта", "bitcoin", "ethereum", "usdt", "казино", "ставки", "букмекер", "промокод", "реферал", "подпишись")
FISHING_RE = re.compile(r"(?:bit\.ly|tinyurl|goo\.gl|t\.me/joinchat|verify.{0,20}wallet|seed.{0,20}phrase)", re.I)
URL_RE = re.compile(r"(?:https?://|www\.|t\.me/|telegram\.me/)", re.I)
INVITE_RE = re.compile(r"(?:t\.me/\+|t\.me/joinchat|telegram\.me/joinchat)", re.I)

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

def user_rank(chat_id: int, user_id: int) -> int:
    row = db_one("bot_users", user_id=user_id, chat_id=chat_id) or {}
    try:
        return max(0, min(5, int(row.get("rank", 0))))
    except (TypeError, ValueError):
        return 0

async def require_admin(message: Message) -> bool:
    if not message.from_user or not is_group(message):
        await message.answer("Эта команда работает в группе.")
        return False
    if await is_admin(bot, message.chat.id, message.from_user.id) or user_rank(message.chat.id, message.from_user.id) >= 1:
        return True
    await message.answer("⛔ Команда доступна администраторам и назначенным модераторам.")
    return False

async def require_real_admin(message: Message) -> bool:
    if not message.from_user or not is_group(message):
        await message.answer("Эта команда работает в группе.")
        return False
    if await is_admin(bot, message.chat.id, message.from_user.id):
        return True
    await message.answer("⛔ Назначать ранги и менять настройки могут только администраторы Telegram.")
    return False

_TIME_UNITS = {
    "с": 1, "сек": 1, "секунда": 1, "секунды": 1, "секунд": 1,
    "s": 1, "sec": 1, "секунду": 1,
    "м": 60, "мин": 60, "минута": 60, "минуты": 60, "минут": 60,
    "m": 60, "min": 60,
    "ч": 3600, "час": 3600, "часа": 3600, "часов": 3600, "часе": 3600,
    "h": 3600, "hr": 3600,
    "д": 86400, "дн": 86400, "день": 86400, "дня": 86400, "дней": 86400,
    "сутки": 86400, "суток": 86400, "day": 86400, "days": 86400, "d": 86400,
    "н": 604800, "нед": 604800, "неделя": 604800, "недели": 604800,
    "недель": 604800, "week": 604800, "weeks": 604800, "w": 604800,
    "мес": 2592000, "месяц": 2592000, "месяца": 2592000, "месяцев": 2592000,
    "month": 2592000, "months": 2592000,
    "г": 31536000, "год": 31536000, "года": 31536000, "лет": 31536000,
    "years": 31536000, "year": 31536000, "y": 31536000,
}

def _unit_seconds(unit: str) -> int | None:
    unit = unit.lower().strip().rstrip(".,")
    if unit in _TIME_UNITS:
        return _TIME_UNITS[unit]
    # Resolve grammatical variants such as "минутой" and long words.
    for key, seconds in sorted(_TIME_UNITS.items(), key=lambda item: len(item[0]), reverse=True):
        if len(key) >= 3 and unit.startswith(key):
            return seconds
    return None

def parse_duration(value: str) -> timedelta | None:
    """Parse one or more duration units: '1 час 30 минут', '2д', '1 год'."""
    parsed = parse_duration_prefix((value or "").split())
    return parsed[0] if parsed else None

def parse_duration_prefix(tokens: list[str]) -> tuple[timedelta, int, str] | None:
    total = 0
    consumed = 0
    shown: list[str] = []
    i = 0
    while i < len(tokens):
        token = tokens[i].strip().lower().strip(",.;")
        m = re.fullmatch(r"(\d{1,7})\s*([a-zа-яё]+)?", token, re.I)
        if not m:
            break
        amount = int(m.group(1))
        unit = m.group(2)
        used = 1
        if not unit and i + 1 < len(tokens):
            unit = tokens[i + 1].lower().strip(",.;")
            seconds = _unit_seconds(unit)
            if seconds is not None:
                used = 2
            else:
                break
        elif unit:
            seconds = _unit_seconds(unit)
            if seconds is None:
                break
        else:
            break
        if amount < 1:
            return None
        total += amount * seconds
        shown.append(f"{amount} {tokens[i + 1] if used == 2 else unit}")
        consumed += used
        i += used
        if i >= len(tokens) or not re.match(r"^\d", tokens[i]):
            break
    # Telegram only accepts temporary restrictions up to about 366 days.
    # A single year is represented as 365 days; longer periods are rejected.
    if consumed == 0 or total <= 0 or total > 366 * 86400:
        return None
    return timedelta(seconds=total), consumed, " ".join(shown)

def split_duration_reason(tokens: list[str]) -> tuple[timedelta | None, str | None, str | None]:
    """Return duration, human-readable duration, and the remaining reason."""
    result = parse_duration_prefix(tokens)
    if not result:
        return None, None, " ".join(tokens).strip() or None
    duration, consumed, label = result
    reason = " ".join(tokens[consumed:]).strip(" ,.;:-") or None
    return duration, label, reason

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
@router.message(F.text.regexp(r"(?i)^[.!/]?(?:помощь|команды)(?:\s|$)"))
@router.message(F.text.in_({"🔗 Команды", "❓ Помощь"}))
async def help_command(message: Message) -> None:
    await message.answer(
        "<b>📚 Команды Pear CM</b>\n\n"
        "<b>Профиль и чат</b>\n"
        "• /profile или профиль — профиль; /top или топ — активные участники\n"
        "• /admins или админы / кто админ — список админов\n"
        "• /warns или варны — мои предупреждения; /marry или брак — брак в ответ на сообщение\n\n"
        "<b>Модерация (команды можно писать по-русски или по-английски)</b>\n"
        "• ответьте на сообщение: «мут 1 час спам» или «/mute 1h spam» — мут на 1 час с причиной\n"
        "• /unmute или размут — снять мут; /ban или бан — бан; /unban или разбан — разбан\n"
        "• Разбан: ответом на сообщение, либо «разбан 123456789» / «/unban 123456789»\n"
        "• /kick или кик — исключить; /warn или варн — выдать предупреждение\n"
        "• /unwarn или анварн — снять одно предупреждение; /clearwarns или сбросварнов — обнулить\n"
        "• /del или «-смс» — удалить сообщение; /purge 10 или «очистить 10» — очистить сообщения\n"
        "• /pin или «.пин» — закрепить сообщение; /unpin или «.анпин» — открепить\n"
        "• /info или инфо — информация об участнике; /logs или логи — последние действия\n"
        "• +антиссылка / -антиссылка — управление фильтрами (и другие +анти…/-анти…)\n"
        "• /settrigger 3 mute 1ч — настройка наказания за варны; /rank — ваш ранг\n"
        "• /time или дата — текущее московское время (Europe/Moscow, UTC+3); /calc 2+2 — калькулятор; /random 1 100 — случайное число; /dice — кубик\n"
        "• /activity — активность; /quote ответом — цитата; /favorites ответом — избранное; /achievements — достижения; /suggest текст — предложение\n\n"
        "<b>Овнер</b>: /owner, /status, /ping, /diag, /logs, /globalstats, /modules, /operator ID, /maintenance on|off\n\n"
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
@router.message(F.text.regexp(r"(?i)^[.!/]?профиль(?:\s|$)"))
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
@router.message(Command("time"))
@router.message(F.text.regexp(r"(?i)^[.!]?время(?:\s|$)"))
async def moscow_time_cmd(message: Message) -> None:
    now = moscow_now()
    await message.answer(
        "🕒 <b>Московское время</b>\n"
        f"{now.strftime('%d.%m.%Y')} · <b>{now.strftime('%H:%M:%S')} МСК</b>\n"
        "Часовой пояс: <code>Europe/Moscow</code> (UTC+3)\n"
        "Время обновляется при каждом вызове команды."
    )


@router.message(Command("warns"))
@router.message(F.text.regexp(r"(?i)^[.!/]?(?:варны|мои варны)(?:\s|$)"))
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
@router.message(F.text.regexp(r"(?i)^[.!/]?(?:админы|кто админ)(?:\s|$)"))
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
    settings = get_chat_settings(message.chat.id) if "get_chat_settings" in globals() else dict(FILTER_DEFAULTS)
    for user in message.new_chat_members:
        if user.is_bot:
            if settings.get("antibot"):
                try:
                    await bot.ban_chat_member(message.chat.id, user.id)
                    audit(message.chat.id, 0, "antibot_ban", user.id, "бот добавлен при включённом антиботе")
                except Exception:
                    log.exception("Could not remove bot blocked by antibot")
            continue
        ensure_user(user.id, message.chat.id, user.username, user.full_name)
        await message.answer(f"👋 Добро пожаловать, {mention(user.id, user.full_name)}!")
    # Process anti-raid after welcoming; this is called directly because aiogram stops at the first matching service-message handler.
    await apply_raid_protection(message)

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
@router.message(F.text.regexp(r"(?i)^[.!/!]?мут(?:\s|$)"))
async def mute_cmd(message: Message) -> None:
    if not await check_mod(message):
        return
    raw_args = args_after_command(message)
    # In reply-mode every argument follows the command. Without a reply, the
    # first argument identifies the target (@username or known numeric ID).
    target_args = [] if message.reply_to_message else raw_args[:1]
    target = await resolve_target(message, target_args)
    if not target:
        await message.answer("Ответьте на сообщение участника или укажите известный чату @username/ID.")
        return
    if message.from_user and target[0] == message.from_user.id:
        await message.answer("Нельзя замутить самого себя.")
        return
    if target[0] == OWNER_ID and message.from_user and message.from_user.id != OWNER_ID:
        await message.answer("⛔ Нельзя замутить владельца бота.")
        return
    duration_tokens = raw_args if message.reply_to_message else raw_args[1:]
    duration, duration_label, reason = split_duration_reason(duration_tokens)
    if duration is None:
        # No recognized duration means a permanent mute; all remaining text is the reason.
        reason = " ".join(duration_tokens).strip() or None
        duration_label = "бессрочно"
        until = None
    else:
        until = datetime.now(timezone.utc) + duration
    reason = reason or "не указана"
    try:
        await bot.restrict_chat_member(
            chat_id=message.chat.id, user_id=target[0],
            permissions=ChatPermissions(can_send_messages=False), until_date=until,
        )
        audit(message.chat.id, message.from_user.id, "mute", target[0], f"duration={duration_label}; reason={reason}")
        if until:
            when = format_moscow_time(until)
            await message.answer(f"🔇 {mention(target[0], target[1])} замучен на <b>{duration_label}</b> по причине: <b>{reason}</b>. До {when}.")
        else:
            await message.answer(f"🔇 {mention(target[0], target[1])} замучен бессрочно по причине: <b>{reason}</b>.")
    except Exception as exc:
        await safe_error(message, exc)

@router.message(Command("unmute"))
@router.message(F.text.regexp(r"(?i)^[.!/! ]?(?:размут|анмут|унмут)(?:\s|$)"))
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
@router.message(F.text.regexp(r"(?i)^[.!/!]?бан(?:\s|$)"))
async def ban_cmd(message: Message) -> None:
    if not await check_mod(message):
        return
    target = await moderation_target(message, "ban")
    if not target:
        return
    raw_args = args_after_command(message)
    reason = " ".join(raw_args if message.reply_to_message else raw_args[1:]).strip() or "не указана"
    try:
        await bot.ban_chat_member(message.chat.id, target[0])
        audit(message.chat.id, message.from_user.id, "ban", target[0], f"reason={reason}")
        await message.answer(f"⛔ {mention(target[0], target[1])} заблокирован в чате. Причина: <b>{reason}</b>.")
    except Exception as exc:
        await safe_error(message, exc)

@router.message(Command("unban"))
@router.message(F.text.regexp(r"(?i)^[.!/]?разбан(?:\s|$)"))
async def unban_cmd(message: Message) -> None:
    if not await check_mod(message):
        return
    args = args_after_command(message)
    target = await resolve_target(message, args)
    if not target:
        await message.answer(
            "Формат: ответьте на сообщение командой <code>разбан</code> / <code>/unban</code> "
            "или укажите ID/известный чату @username: <code>разбан 123456789</code>."
        )
        return
    try:
        await bot.unban_chat_member(message.chat.id, target[0], only_if_banned=True)
        audit(message.chat.id, message.from_user.id, "unban", target[0], "manual unban")
        await message.answer(f"✅ {mention(target[0], target[1])} разблокирован в этом чате.")
    except Exception as exc:
        await safe_error(message, exc)

@router.message(Command("kick"))
@router.message(F.text.regexp(r"(?i)^[.!/]?кик(?:\s|$)"))
@router.message(F.text.regexp(r"(?i)^[.!/!]?кик(?:\s|$)"))
async def kick_cmd(message: Message) -> None:
    if not await check_mod(message):
        return
    target = await moderation_target(message, "kick")
    if not target:
        return
    raw_args = args_after_command(message)
    reason = " ".join(raw_args if message.reply_to_message else raw_args[1:]).strip() or "не указана"
    try:
        await bot.ban_chat_member(message.chat.id, target[0])
        await bot.unban_chat_member(message.chat.id, target[0], only_if_banned=True)
        audit(message.chat.id, message.from_user.id, "kick", target[0], f"reason={reason}")
        await message.answer(f"👢 {mention(target[0], target[1])} исключён из чата. Причина: <b>{reason}</b>.")
    except Exception as exc:
        await safe_error(message, exc)

@router.message(Command("warn"))
@router.message(F.text.regexp(r"(?i)^[.!/]?варн(?:\s|$)"))
@router.message(F.text.regexp(r"(?i)^[.!/!]?варн(?:\s|$)"))
async def warn_cmd(message: Message) -> None:
    if not await require_admin(message):
        return
    raw_args = args_after_command(message)
    target = await resolve_target(message, [] if message.reply_to_message else raw_args[:1])
    if not target:
        await message.answer("Ответьте на сообщение участника или укажите известный чату @username/ID.")
        return
    if message.from_user and target[0] == message.from_user.id:
        await message.answer("Нельзя выдать варн самому себе.")
        return
    reason = " ".join(raw_args if message.reply_to_message else raw_args[1:]).strip() or "не указана"
    count = set_warns(message.chat.id, target[0], 1)
    audit(message.chat.id, message.from_user.id, "warn", target[0], f"warns={count}; reason={reason}")
    await message.answer(f"⚠️ {mention(target[0], target[1])} получил предупреждение <b>{count}</b> по причине: <b>{reason}</b>.")
    settings = get_chat_settings(message.chat.id)
    thresholds = [(10, "warn_10"), (7, "warn_7"), (5, "warn_5"), (3, "warn_3")]
    for threshold, key in thresholds:
        if count >= threshold:
            action = str(settings.get(f"{key}_action", "mute"))
            time_label = str(settings.get(f"{key}_time", "1ч"))
            if action == "ban":
                try:
                    await bot.ban_chat_member(message.chat.id, target[0])
                    audit(message.chat.id, message.from_user.id, "auto_ban", target[0], f"{count} warns; reason={reason}")
                    await message.answer(f"⛔ Автобан {mention(target[0], target[1])}: {count} предупреждений.")
                except Exception as exc:
                    await safe_error(message, exc)
            elif action == "mute" and await bot_can_restrict(message):
                parsed = parse_duration(time_label)
                until = datetime.now(timezone.utc) + (parsed or timedelta(days=365))
                try:
                    await bot.restrict_chat_member(message.chat.id, target[0], permissions=ChatPermissions(can_send_messages=False), until_date=until)
                    audit(message.chat.id, message.from_user.id, "auto_mute", target[0], f"{count} warns; time={time_label}")
                    await message.answer(f"🔇 Автомут {mention(target[0], target[1])} на {time_label}: {count} предупреждений.")
                except Exception as exc:
                    await safe_error(message, exc)
            break

@router.message(Command("unwarn"))
@router.message(F.text.regexp(r"(?i)^[.!/]?(?:анварн|снятьварн)(?:\s|$)"))
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
@router.message(F.text.regexp(r"(?i)^[.!/]?(?:сбросварнов|обнулитьварны)(?:\s|$)"))
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
@router.message(F.text.regexp(r"(?i)^-смс(?:\s+\d+)?$"))
async def delete_message_cmd(message: Message) -> None:
    if not await require_admin(message):
        return

    raw = (message.text or "").strip()
    match = re.fullmatch(r"(?i)-смс(?:\s+(\d+))?", raw)

    # -смс N удаляет N последних сообщений ДО команды. Саму команду удаляем отдельно.
    # Telegram не предоставляет историю сообщений боту, поэтому проверяем предыдущие
    # message_id и пропускаем удалённые/недоступные сообщения, пока не наберём N.
    if match and match.group(1):
        try:
            requested = int(match.group(1))
        except ValueError:
            requested = 10
        count = max(1, min(100, requested))
        deleted = 0
        forbidden = False
        scanned = 0
        scan_limit = min(2000, max(100, count * 20))
        mid = message.message_id - 1

        while mid >= 1 and deleted < count and scanned < scan_limit:
            scanned += 1
            try:
                await bot.delete_message(chat_id=message.chat.id, message_id=mid)
                deleted += 1
            except TelegramForbiddenError:
                forbidden = True
                break
            except TelegramBadRequest:
                # ID может относиться к уже удалённому или недоступному сообщению.
                pass
            except Exception:
                log.exception("Ошибка удаления сообщения %s в чате %s", mid, message.chat.id)
            mid -= 1

        try:
            await message.delete()
        except TelegramForbiddenError:
            forbidden = True
        except Exception:
            log.debug("Не удалось удалить команду -смс", exc_info=True)

        audit(
            message.chat.id, message.from_user.id, "purge_recent",
            details=f"requested={count};deleted={deleted};scanned={scanned};forbidden={forbidden}",
        )
        if forbidden:
            response = await message.answer(
                "⛔ Не хватает прав на удаление сообщений. Назначьте бота администратором "
                "и включите право «Удаление сообщений»."
            )
        else:
            response = await message.answer(
                f"🧹 Удалено предыдущих сообщений: <b>{deleted}</b> из {count}."
                + (" Не все сообщения доступны для удаления." if deleted < count else "")
            )
        await asyncio.sleep(4)
        try:
            await response.delete()
        except Exception:
            pass
        return

    target_message = message.reply_to_message
    if not target_message:
        await message.answer(
            "Ответьте на сообщение командой <code>/del</code> или напишите "
            "<code>-смс 10</code>, чтобы удалить 10 последних сообщений."
        )
        return
    try:
        await target_message.delete()
        await message.delete()
        audit(
            message.chat.id, message.from_user.id, "delete",
            target_message.from_user.id if target_message.from_user else None,
        )
    except Exception as exc:
        await safe_error(message, exc)

@router.message(Command("purge"))
@router.message(F.text.regexp(r"(?i)^[.!/]?(?:очистить|чистка)(?:\s|$)"))
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
@router.message(F.text.regexp(r"(?i)^[.!/]?инфо(?:\s|$)"))
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
@router.message(F.text.regexp(r"(?i)^[.!/]?топ(?:\s|$)"))
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
@router.message(F.text.regexp(r"(?i)^[.!/]?логи(?:\s|$)"))
async def logs_cmd(message: Message) -> None:
    # In private chat, allow the configured owner to inspect global audit logs.
    if message.chat.type == ChatType.PRIVATE and message.from_user and message.from_user.id == OWNER_ID:
        rows = db_rows("bot_audit", limit=15, order="created_at")
        if not rows:
            await message.answer("📒 Глобальные логи пусты или bot_audit недоступна.")
            return
        lines = ["📒 <b>Глобальные действия</b>"]
        for row in rows:
            created = str(row.get("created_at") or "")
            try:
                created = format_moscow_time(datetime.fromisoformat(created.replace("Z", "+00:00")))
            except Exception:
                pass
            details = (row.get("details") or "—").replace("<", "&lt;").replace(">", "&gt;")[:160]
            lines.append(f"• <code>{created}</code> · {row.get('action','?')} · actor <code>{row.get('actor_id','?')}</code> · target <code>{row.get('target_id') or '—'}</code>\n  {details}")
        await message.answer("\n".join(lines))
        return
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
@router.message(F.text.regexp(r"(?i)^[.!/]?брак(?:\s|$)"))
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
@router.message(F.text.regexp(r"(?i)^[.!/]?(?:развод|развестись)(?:\s|$)"))
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

# ── Integrated moderation stages from moderation.py ──────────────────────────
# Stage A: durable per-chat filter settings and human-readable time/reason parsing.
def get_chat_settings(chat_id: int) -> dict[str, Any]:
    if chat_id in _CHAT_SETTINGS_CACHE:
        return _CHAT_SETTINGS_CACHE[chat_id]
    settings = dict(FILTER_DEFAULTS)
    row = db_one("bot_chat_settings", chat_id=chat_id)
    if row and isinstance(row.get("settings"), dict):
        settings.update(row["settings"])
    _CHAT_SETTINGS_CACHE[chat_id] = settings
    return settings

def save_chat_settings(chat_id: int, settings: dict[str, Any]) -> None:
    _CHAT_SETTINGS_CACHE[chat_id] = dict(settings)
    db_upsert("bot_chat_settings", {
        "chat_id": chat_id, "settings": settings,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }, "chat_id")

FILTER_ALIASES = {
    "антиссылка": "antilink", "антилинк": "antilink",
    "антиинвайт": "antiinvite", "антивызов": "antiinvite",
    "антимат": "antimat", "антифлуд": "antiflood", "антикапс": "anticaps",
    "антиреклама": "antireklama", "антикрипта": "antikripta",
    "антифишинг": "antifishing", "антиолл": "antiall", "антиалл": "antiall",
    "антибот": "antibot", "антистикер": "antisticker", "антигиф": "antigif",
    "антифото": "antiphoto", "антивидео": "antivideo", "антиголос": "antivoice",
    "антидок": "antidocument", "антидокумент": "antidocument",
}

@router.message(F.text.regexp(r"(?i)^[+-](?:антиссылка|антилинк|антиинвайт|антивызов|антимат|антифлуд|антикапс|антиреклама|антикрипта|антифишинг|антиолл|антиалл|антибот|антистикер|антигиф|антифото|антивидео|антиголос|антидок|антидокумент)(?:\s|$)"))
async def filter_toggle_cmd(message: Message) -> None:
    if not await require_real_admin(message):
        return
    parts = (message.text or "").split()
    sign_name = parts[0]
    enabled = sign_name.startswith("+")
    alias = sign_name[1:].lower()
    key = FILTER_ALIASES.get(alias)
    if not key:
        return
    settings = get_chat_settings(message.chat.id)
    settings[key] = enabled
    save_chat_settings(message.chat.id, settings)
    await message.answer(f"{'✅ Включён' if enabled else '🛑 Выключен'} фильтр <b>{alias}</b>.")

@router.message(F.text.regexp(r"(?i)^[.!/]?(?:ранг|rank)(?:\s|$)"))
async def rank_cmd(message: Message) -> None:
    parts = (message.text or "").split()
    if len(parts) == 1:
        if not message.from_user:
            return
        await message.answer(f"🏅 Ваш ранг: <b>{user_rank(message.chat.id, message.from_user.id)}</b> (0 — участник, 1 — модератор, 5 — создатель).")
        return
    if not await require_real_admin(message):
        return
    target = await resolve_target(message, [] if message.reply_to_message else parts[1:2])
    rank_arg_index = 1 if message.reply_to_message else 2
    if not target or len(parts) <= rank_arg_index or not parts[rank_arg_index].isdigit():
        await message.answer("Формат: ответьте на участника и напишите <code>ранг 1</code> либо <code>ранг @username 1</code>.")
        return
    rank = int(parts[rank_arg_index])
    if rank < 0 or rank > 5:
        await message.answer("Ранг должен быть от 0 до 5.")
        return
    user = db_one("bot_users", user_id=target[0], chat_id=message.chat.id) or {}
    db_upsert("bot_users", {**user, "user_id": target[0], "chat_id": message.chat.id,
        "username": user.get("username"), "first_name": user.get("first_name") or target[1],
        "warns": int(user.get("warns", 0)), "messages": int(user.get("messages", 0)), "rank": rank}, "user_id,chat_id")
    audit(message.chat.id, message.from_user.id, "set_rank", target[0], f"rank={rank}")
    await message.answer(f"🏅 Ранг {mention(target[0], target[1])} установлен: <b>{rank}</b>.")

@router.message(F.text.regexp(r"(?i)^[.!/]?(?:настройки фильтров|фильтры|filters)(?:\s|$)"))
async def filters_status_cmd(message: Message) -> None:
    settings = get_chat_settings(message.chat.id)
    enabled = [name for name, key in FILTER_ALIASES.items() if settings.get(key)]
    await message.answer("🛡 <b>Фильтры чата</b>\n" + ("\n".join(f"• {name}: включён" for name in sorted(set(enabled))) if enabled else "Нет включённых фильтров.") + "\n\nВкл: <code>+антиссылка</code>; выкл: <code>-антиссылка</code>.")

@router.message(Command("settrigger"))
async def set_trigger_cmd(message: Message) -> None:
    if not await require_real_admin(message):
        return
    parts = args_after_command(message)
    if len(parts) < 3 or parts[0] not in {"3", "5", "7", "10"} or parts[1] not in {"mute", "ban", "off"}:
        await message.answer("Формат: <code>/settrigger 3 mute 1ч</code>, <code>/settrigger 7 ban</code> или <code>/settrigger 5 off</code>.")
        return
    threshold, action = parts[0], parts[1]
    settings = get_chat_settings(message.chat.id)
    settings[f"warn_{threshold}_action"] = "off" if action == "off" else action
    if action != "off" and len(parts) >= 3:
        settings[f"warn_{threshold}_time"] = " ".join(parts[2:])
    save_chat_settings(message.chat.id, settings)
    await message.answer(f"✅ Триггер {threshold} варнов обновлён: {action}.")

async def auto_moderation_filters(message: Message) -> None:
    if not message.from_user or message.from_user.is_bot or not is_group(message):
        return
    # This handler is the catch-all for group messages, so activity is recorded
    # here (a handler registered earlier would swallow updates in aiogram 3).
    await track_activity(message)
    # In maintenance mode, suppress ordinary group messages but keep commands reachable.
    if message.from_user.id != OWNER_ID:
        if str(_global_setting("maintenance", "off")).lower() == "on" and not (message.text or "").lstrip().startswith(("/", "!", ".")):
            try: await message.delete()
            except Exception: pass
            return
        banned = db_one("bot_global_bans", user_id=message.from_user.id, active=True)
        if banned and not (message.text or "").lstrip().startswith(("/", "!", ".")):
            try: await message.delete()
            except Exception: pass
            return
    # Never filter commands; moderation/admin commands must remain reachable.
    text_value = message.text or message.caption or ""
    if text_value and re.match(r"^\s*(?:[/!.+-]|мут(?:\s|$)|бан(?:\s|$)|кик(?:\s|$)|варн(?:\s|$)|размут(?:\s|$))", text_value, re.I):
        return
    if await is_admin(bot, message.chat.id, message.from_user.id) or user_rank(message.chat.id, message.from_user.id) >= 1:
        return
    settings = get_chat_settings(message.chat.id)
    reason = None
    # Duplicate-message anti-spam is scoped to this chat and user; no cross-chat bleed.
    if settings.get("antispam") and text_value:
        key = (message.chat.id, message.from_user.id)
        normalized = re.sub(r"\s+", " ", text_value.lower()).strip()
        previous = _SPAM_LAST.get(key)
        now_mono = time.monotonic()
        if previous and previous[0] == normalized and now_mono - previous[1] <= 30:
            reason = "повторяющийся спам"
        _SPAM_LAST[key] = (normalized[:1000], now_mono)
    if settings.get("antibot") and message.new_chat_members and any(u.is_bot for u in message.new_chat_members):
        reason = "добавление бота"
    if text_value and not reason:
        if settings.get("antiinvite") and INVITE_RE.search(text_value):
            reason = "пригласительная ссылка"
        elif settings.get("antilink") and URL_RE.search(text_value):
            reason = "ссылка"
        elif settings.get("antifishing") and FISHING_RE.search(text_value):
            reason = "фишинг"
        elif settings.get("antimat") and any(word in text_value.lower() for word in BAD_WORDS):
            reason = "нецензурная лексика"
        elif settings.get("antireklama") and any(word in text_value.lower() for word in AD_WORDS):
            reason = "реклама"
        elif settings.get("antikripta") and any(word in text_value.lower() for word in ("bitcoin", "btc", "ethereum", "usdt", "крипта", "binance", "bybit", "кошелёк", "wallet")):
            reason = "криптореклама"
        elif settings.get("antiall") and re.search(r"@(all|everyone|here)\b", text_value, re.I):
            reason = "массовое упоминание"
        elif settings.get("anticaps"):
            letters = [c for c in text_value if c.isalpha()]
            if len(letters) >= 8 and sum(c.isupper() for c in letters) / len(letters) * 100 >= int(settings.get("caps_percent", 70)):
                reason = "капс"
        if settings.get("antiflood") and not reason:
            key = (message.chat.id, message.from_user.id)
            now = time.monotonic()
            times = _FLOOD_TIMES[key]
            times.append(now)
            window = max(2, min(60, int(settings.get("flood_window", 10))))
            while times and now - times[0] > window:
                times.popleft()
            if len(times) >= max(3, min(20, int(settings.get("flood_limit", 5)))):
                reason = "флуд"
                times.clear()
    if not reason:
        if settings.get("antisticker") and message.sticker:
            reason = "стикер запрещён"
        elif settings.get("antigif") and message.animation:
            reason = "GIF запрещён"
        elif settings.get("antiphoto") and message.photo:
            reason = "фото запрещено"
        elif settings.get("antivideo") and (message.video or message.video_note):
            reason = "видео запрещено"
        elif settings.get("antivoice") and (message.voice or message.audio):
            reason = "голосовые/аудио запрещены"
        elif settings.get("antidocument") and message.document:
            reason = "документы запрещены"
    if not reason:
        return
    try:
        await message.delete()
    except Exception:
        log.warning("Could not delete filtered message in %s", message.chat.id)
    audit(message.chat.id, 0, "filter_delete", message.from_user.id, reason)
    try:
        mute_minutes = max(1, min(1440, int(settings.get("filter_mute_minutes", 10))))
        if await bot_can_restrict(message):
            await bot.restrict_chat_member(message.chat.id, message.from_user.id,
                permissions=ChatPermissions(can_send_messages=False),
                until_date=datetime.now(timezone.utc) + timedelta(minutes=mute_minutes))
            audit(message.chat.id, 0, "filter_mute", message.from_user.id, f"reason={reason}; minutes={mute_minutes}")
        await message.answer(f"🚫 {mention(message.from_user.id, message.from_user.full_name)}: сообщение удалено. Причина: <b>{reason}</b>.")
    except Exception:
        log.exception("Filter punishment failed")

# Important: the old track_activity catch-all handler was registered before
# moderation and consumed ordinary group updates. Register it last so command
# handlers and automatic filters can actually run first.
# ── Pear CM moderation stage: history, reports, appeals, settings, rights, raid ──
# State is per chat; durable settings and case records are stored in Supabase.
@router.message(Command("history"))
@router.message(F.text.regexp(r"(?i)^\s*(?:история наказаний|нарушения)(?:\s|$)"))
async def moderation_history_cmd(message: Message) -> None:
    if not await require_admin(message):
        return
    args = args_after_command(message)
    target_id = None
    if message.reply_to_message and message.reply_to_message.from_user:
        target_id = message.reply_to_message.from_user.id
    else:
        target_match = re.search(r"(?i)(?:история\s+наказаний|нарушения|/history)\s+(\d{4,})", message.text or "")
        if target_match:
            target_id = int(target_match.group(1))
        elif args and args[0].isdigit():
            target_id = int(args[0])
    rows = db_rows("bot_audit", limit=200, order="created_at", chat_id=message.chat.id)
    if target_id is not None:
        rows = [r for r in rows if int(r.get("target_id") or 0) == target_id]
    action_set = {"mute", "unmute", "ban", "unban", "kick", "warn", "clearwarns", "purge", "purge_recent", "delete"}
    rows = [r for r in rows if str(r.get("action", "")).lower() in action_set]
    if not rows:
        await message.answer("📜 История наказаний не найдена (или таблица bot_audit ещё не создана).")
        return
    lines = []
    for row in rows[:15]:
        stamp = str(row.get("created_at") or "время неизвестно")
        try:
            dt = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            stamp = format_moscow_time(dt)
        except Exception:
            pass
        details = str(row.get("details") or "—").replace("<", "&lt;").replace(">", "&gt;")[:100]
        lines.append(f"• <code>{stamp}</code> · <b>{row.get('action','?')}</b> · модератор <code>{row.get('actor_id','?')}</code> · цель <code>{row.get('target_id') or '—'}</code>\n  {details}")
    await message.answer("<b>📜 История наказаний</b>\n" + "\n".join(lines))

@router.message(Command("auditlog"))
@router.message(F.text.regexp(r"(?i)^\s*аудит(?:\s|$)"))
async def audit_search_cmd(message: Message) -> None:
    if not await require_admin(message):
        return
    args = args_after_command(message)
    moderator_id = int(args[0]) if args and args[0].isdigit() else None
    hours = int(args[1]) if len(args) > 1 and args[1].isdigit() else 24
    hours = max(1, min(hours, 720))
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    rows = db_rows("bot_audit", limit=200, order="created_at", chat_id=message.chat.id)
    result = []
    for row in rows:
        if moderator_id is not None and int(row.get("actor_id") or 0) != moderator_id:
            continue
        try:
            stamp = datetime.fromisoformat(str(row.get("created_at", "")).replace("Z", "+00:00"))
            if stamp.tzinfo is None:
                stamp = stamp.replace(tzinfo=timezone.utc)
            if stamp < cutoff:
                continue
        except Exception:
            continue
        result.append(row)
    if not result:
        await message.answer(f"🔎 За последние {hours} ч записей не найдено.")
        return
    lines = []
    for row in result[:15]:
        stamp = str(row.get("created_at") or "—")
        try:
            stamp = format_moscow_time(datetime.fromisoformat(stamp.replace("Z", "+00:00")))
        except Exception:
            pass
        detail = str(row.get("details") or "—").replace("<", "&lt;").replace(">", "&gt;")[:90]
        lines.append(f"• <code>{stamp}</code> · {row.get('action','?')} · модератор <code>{row.get('actor_id','?')}</code> · цель <code>{row.get('target_id') or '—'}</code> · {detail}")
    await message.answer("<b>🔎 Аудит действий</b>\n" + "\n".join(lines))

@router.message(Command("report", "жалоба", "репорт"))
@router.message(F.text.regexp(r"(?i)^\s*(?:жалоба|репорт)\s+\S"))
async def submit_report_cmd(message: Message) -> None:
    if not message.from_user:
        return
    body = " ".join(args_after_command(message)).strip()
    target_id = message.reply_to_message.from_user.id if message.reply_to_message and message.reply_to_message.from_user else None
    if not body and target_id is None:
        await message.answer("Использование: <code>репорт описание</code> или ответьте на сообщение командой <code>репорт причина</code>.")
        return
    payload = {
        "chat_id": message.chat.id, "user_id": message.from_user.id,
        "target_id": target_id, "body": (body or "Жалоба на сообщение в ответе")[:2000],
        "status": "open", "created_at": datetime.now(timezone.utc).isoformat(),
    }
    row = db_insert("bot_reports", payload)
    if row:
        audit(message.chat.id, message.from_user.id, "report_created", target_id, f"report_id={row.get('id')}")
        await message.answer(f"📨 Жалоба принята. Номер: <code>{row.get('id','создан')}</code>.")
    else:
        await message.answer("Не удалось сохранить жалобу. Проверьте таблицу bot_reports из schema.sql.")

@router.message(Command("appeal", "апелляция"))
@router.message(F.text.regexp(r"(?i)^\s*апелляция\s+\S"))
async def submit_appeal_cmd(message: Message) -> None:
    if not message.from_user:
        return
    body = " ".join(args_after_command(message)).strip()
    if not body:
        await message.answer("Использование: <code>апелляция Прошу пересмотреть наказание</code>.")
        return
    row = db_insert("bot_appeals", {
        "chat_id": message.chat.id, "user_id": message.from_user.id,
        "body": body[:2000], "status": "open",
        "created_at": datetime.now(timezone.utc).isoformat(),
    })
    if row:
        audit(message.chat.id, message.from_user.id, "appeal_created", message.from_user.id, f"appeal_id={row.get('id')}")
        await message.answer(f"📨 Апелляция отправлена администрации. Номер: <code>{row.get('id','создан')}</code>.")
    else:
        await message.answer("Не удалось сохранить апелляцию. Проверьте таблицу bot_appeals из schema.sql.")

@router.message(Command("reports", "жалобы"))
@router.message(F.text.regexp(r"(?i)^\s*жалобы\s*$"))
async def list_reports_cmd(message: Message) -> None:
    if not await require_admin(message):
        return
    rows = db_rows("bot_reports", limit=20, order="created_at", chat_id=message.chat.id, status="open")
    if not rows:
        await message.answer("📭 Открытых жалоб нет или таблица bot_reports не настроена.")
        return
    lines = [f"• #{r.get('id')} · от <code>{r.get('user_id')}</code> · цель <code>{r.get('target_id') or '—'}</code>\n  {str(r.get('body',''))[:180]}" for r in rows]
    await message.answer("<b>📨 Открытые жалобы</b>\n" + "\n".join(lines) + "\n\nЗакрыть: <code>/closereport ID причина</code>")

@router.message(Command("appeals", "апелляции"))
@router.message(F.text.regexp(r"(?i)^\s*апелляции\s*$"))
async def list_appeals_cmd(message: Message) -> None:
    if not await require_admin(message):
        return
    rows = db_rows("bot_appeals", limit=20, order="created_at", chat_id=message.chat.id, status="open")
    if not rows:
        await message.answer("📭 Открытых апелляций нет или таблица bot_appeals не настроена.")
        return
    lines = [f"• #{r.get('id')} · пользователь <code>{r.get('user_id')}</code>\n  {str(r.get('body',''))[:180]}" for r in rows]
    await message.answer("<b>📨 Открытые апелляции</b>\n" + "\n".join(lines) + "\n\nЗакрыть: <code>/closeappeal ID причина</code>")

async def close_case(message: Message, table: str, action: str) -> None:
    if not await require_admin(message):
        return
    text_value = (message.text or "").strip()
    phrase = r"(?:/close(?:report|appeal)|закрыть\s+(?:жалобу|апелляцию)|закрыть(?:жалобу|апелляцию))"
    match = re.match(rf"(?is)^\s*{phrase}\s+(\d+)\s+(.+)$", text_value)
    if not match:
        args = args_after_command(message)
        if len(args) < 2 or not args[0].isdigit():
            await message.answer("Формат: <code>/closereport ID причина</code> или <code>закрыть жалобу ID причина</code>.")
            return
        case_id, reason = int(args[0]), " ".join(args[1:])[:500]
    else:
        case_id, reason = int(match.group(1)), match.group(2).strip()[:500]
    # Supabase helper intentionally limits updates to a single matching row.
    if db is None:
        await message.answer("База данных недоступна.")
        return
    try:
        result = db.table(table).update({
            "status": "closed", "closed_by": message.from_user.id,
            "closed_reason": reason, "closed_at": datetime.now(timezone.utc).isoformat(),
        }).eq("id", case_id).eq("chat_id", message.chat.id).eq("status", "open").execute()
        if not result.data:
            await message.answer("Открытая запись с таким ID в этом чате не найдена.")
            return
        audit(message.chat.id, message.from_user.id, action, None, f"id={case_id}; reason={reason}")
        await message.answer(f"✅ Обращение #{case_id} закрыто. Причина: {reason}")
    except Exception as exc:
        await safe_error(message, exc)

@router.message(Command("closereport", "закрытьжалобу"))
@router.message(F.text.regexp(r"(?i)^\s*закрыть жалобу(?:\s|$)"))
async def close_report_cmd(message: Message) -> None:
    await close_case(message, "bot_reports", "report_closed")

@router.message(Command("closeappeal", "закрытьапелляцию"))
@router.message(F.text.regexp(r"(?i)^\s*закрыть апелляцию(?:\s|$)"))
async def close_appeal_cmd(message: Message) -> None:
    await close_case(message, "bot_appeals", "appeal_closed")

@router.message(Command("antiflood"))
@router.message(F.text.regexp(r"(?i)^\s*антифлуд(?:\s|$)"))
async def configure_antiflood_cmd(message: Message) -> None:
    if not await require_real_admin(message):
        return
    args = args_after_command(message)
    settings = get_chat_settings(message.chat.id)
    if args and args[0].lower() in ("вкл", "on", "включить"):
        settings["antiflood"] = True
    elif args and args[0].lower() in ("выкл", "off", "выключить"):
        settings["antiflood"] = False
    elif len(args) >= 2 and args[0].isdigit() and args[1].isdigit():
        settings["flood_limit"] = max(3, min(20, int(args[0])))
        settings["flood_window"] = max(2, min(60, int(args[1])))
        settings["antiflood"] = True
    elif not args:
        await message.answer(f"Антифлуд: {'вкл' if settings.get('antiflood') else 'выкл'}; лимит {settings.get('flood_limit',5)} сообщений за {settings.get('flood_window',10)} сек.\nПример: <code>антифлуд 5 10</code>, <code>антифлуд вкл</code>, <code>антифлуд выкл</code>.")
        return
    else:
        await message.answer("Формат: <code>антифлуд 5 10</code> или <code>антифлуд вкл/выкл</code>.")
        return
    save_chat_settings(message.chat.id, settings)
    audit(message.chat.id, message.from_user.id, "antiflood_settings", details=f"enabled={settings['antiflood']}; limit={settings.get('flood_limit')}; window={settings.get('flood_window')}")
    await message.answer(f"✅ Антифлуд {'включён' if settings['antiflood'] else 'выключен'}. Лимит: {settings.get('flood_limit',5)} за {settings.get('flood_window',10)} сек.")

@router.message(Command("antispam"))
@router.message(F.text.regexp(r"(?i)^\s*антиспам(?:\s|$)"))
async def configure_antispam_cmd(message: Message) -> None:
    if not await require_real_admin(message):
        return
    args = args_after_command(message)
    settings = get_chat_settings(message.chat.id)
    if args and args[0].lower() in ("вкл", "on", "включить"):
        settings["antispam"] = True
    elif args and args[0].lower() in ("выкл", "off", "выключить"):
        settings["antispam"] = False
    elif not args:
        await message.answer(f"Антиспам: {'вкл' if settings.get('antispam') else 'выкл'}.\nПример: <code>антиспам вкл</code> или <code>антиспам выкл</code>.")
        return
    else:
        await message.answer("Формат: <code>антиспам вкл/выкл</code>.")
        return
    save_chat_settings(message.chat.id, settings)
    audit(message.chat.id, message.from_user.id, "antispam_settings", details=f"enabled={settings['antispam']}")
    await message.answer(f"✅ Антиспам {'включён' if settings['antispam'] else 'выключен'}.")

@router.message(Command("antiraid"))
@router.message(F.text.regexp(r"(?i)^\s*антирейд(?:\s|$)"))
async def configure_antiraid_cmd(message: Message) -> None:
    if not await require_real_admin(message):
        return
    args = args_after_command(message)
    settings = get_chat_settings(message.chat.id)
    if args and args[0].lower() in ("вкл", "on", "включить"):
        settings["antiraid"] = True
    elif args and args[0].lower() in ("выкл", "off", "выключить"):
        settings["antiraid"] = False
    elif len(args) >= 2 and args[0].isdigit() and args[1].isdigit():
        settings["raid_threshold"] = max(3, min(50, int(args[0])))
        settings["raid_window"] = max(10, min(300, int(args[1])))
        if len(args) >= 3 and args[2].isdigit():
            settings["raid_restrict_minutes"] = max(1, min(30, int(args[2])))
        settings["antiraid"] = True
    elif not args:
        await message.answer(f"Антирейд: {'вкл' if settings.get('antiraid') else 'выкл'}; порог {settings.get('raid_threshold',8)} вступлений за {settings.get('raid_window',30)} сек.; ограничение {settings.get('raid_restrict_minutes',2)} мин.\nПример: <code>антирейд 8 30 2</code> или <code>антирейд выкл</code>.")
        return
    else:
        await message.answer("Формат: <code>антирейд 8 30 2</code> или <code>антирейд вкл/выкл</code>.")
        return
    save_chat_settings(message.chat.id, settings)
    audit(message.chat.id, message.from_user.id, "antiraid_settings", details=f"enabled={settings['antiraid']}; threshold={settings.get('raid_threshold')}; window={settings.get('raid_window')}")
    await message.answer(f"✅ Антирейд {'включён' if settings['antiraid'] else 'выключен'}.")

@router.message(Command("checkrights", "проверкаправа"))
@router.message(F.text.regexp(r"(?i)^\s*проверка прав\s*$"))
async def check_rights_cmd(message: Message) -> None:
    if not is_group(message):
        await message.answer("Команда работает в группе.")
        return
    lines = ["<b>🔐 Проверка прав</b>"]
    try:
        me = await bot.get_me()
        member = await bot.get_chat_member(message.chat.id, me.id)
        status = getattr(member, "status", "unknown")
        lines.append(f"Бот: <b>{status}</b>")
        if status not in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR):
            lines.append("❌ Бот не администратор.")
        else:
            for field, label in [
                ("can_delete_messages", "удалять сообщения"),
                ("can_restrict_members", "муты и баны"),
                ("can_pin_messages", "закреплять сообщения"),
                ("can_invite_users", "приглашать пользователей"),
            ]:
                value = getattr(member, field, None)
                lines.append(f"{'✅' if value else '❌'} {label}")
    except Exception as exc:
        lines.append(f"Не удалось получить права бота: {type(exc).__name__}")
    if message.from_user:
        try:
            actor = await bot.get_chat_member(message.chat.id, message.from_user.id)
            lines.append(f"Вы: <b>{getattr(actor, 'status', 'unknown')}</b>")
        except Exception:
            lines.append("Права вызывающего пользователя определить не удалось.")
    await message.answer("\n".join(lines))

async def apply_raid_protection(message: Message) -> None:
    settings = get_chat_settings(message.chat.id)
    if not settings.get("antiraid"):
        return
    now = time.monotonic()
    joins = _RAID_JOINS[message.chat.id]
    joins.extend([now] * len(message.new_chat_members))
    window = max(10, min(300, int(settings.get("raid_window", 30))))
    while joins and now - joins[0] > window:
        joins.popleft()
    threshold = max(3, min(50, int(settings.get("raid_threshold", 8))))
    if len(joins) < threshold:
        return
    minutes = max(1, min(30, int(settings.get("raid_restrict_minutes", 2))))
    restricted = 0
    for user in message.new_chat_members:
        if user.is_bot:
            continue
        try:
            if await is_admin(bot, message.chat.id, user.id):
                continue
            await bot.restrict_chat_member(
                chat_id=message.chat.id, user_id=user.id,
                permissions=ChatPermissions(
                    can_send_messages=False, can_send_audios=False, can_send_documents=False,
                    can_send_photos=False, can_send_videos=False, can_send_video_notes=False,
                    can_send_voice_notes=False, can_send_polls=False,
                    can_send_other_messages=False, can_add_web_page_previews=False,
                ),
                until_date=datetime.now(timezone.utc) + timedelta(minutes=minutes),
            )
            restricted += 1
            audit(message.chat.id, 0, "raid_restrict", user.id, f"threshold={threshold}; window={window}s; minutes={minutes}")
        except Exception:
            log.exception("Raid restriction failed")
    joins.clear()
    if restricted:
        await message.answer(f"🛡 Обнаружен возможный рейд: временно ограничено новых участников: <b>{restricted}</b> на {minutes} мин. Проверьте настройки и права бота.")




# ── Chat open/close commands (Russian Iris-style aliases) ────────────────────
@router.message(Command("openchat", "unlockchat"))
@router.message(F.text.regexp(r"(?i)^\s*\+чат\s*$"))
async def open_chat_cmd(message: Message) -> None:
    if not is_group(message):
        await message.answer("Команда работает только в группе.")
        return
    if not await require_real_admin(message):
        return
    settings = get_chat_settings(message.chat.id)
    saved = settings.get("chat_permissions_before_lock")
    try:
        if isinstance(saved, dict):
            permissions = ChatPermissions(**saved)
        else:
            # If this bot did not save a prior permission set, restore the common
            # group permissions without granting admin privileges.
            permissions = ChatPermissions(
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
            )
        await bot.set_chat_permissions(message.chat.id, permissions=permissions)
        settings.pop("chat_permissions_before_lock", None)
        settings["chat_locked_by_bot"] = False
        save_chat_settings(message.chat.id, settings)
        audit(message.chat.id, message.from_user.id, "chat_open")
        await message.answer("🔓 Чат открыт. Отправка сообщений снова разрешена согласно восстановленным настройкам.")
    except Exception as exc:
        await safe_error(message, exc)

@router.message(Command("closechat", "lockchat"))
@router.message(F.text.regexp(r"(?i)^\s*-чат\s*$"))
async def close_chat_cmd(message: Message) -> None:
    if not is_group(message):
        await message.answer("Команда работает только в группе.")
        return
    if not await require_real_admin(message):
        return
    settings = get_chat_settings(message.chat.id)
    try:
        if not settings.get("chat_locked_by_bot"):
            chat_info = await bot.get_chat(message.chat.id)
            current = getattr(chat_info, "permissions", None)
            if current is not None:
                fields = (
                    "can_send_messages", "can_send_audios", "can_send_documents",
                    "can_send_photos", "can_send_videos", "can_send_video_notes",
                    "can_send_voice_notes", "can_send_polls", "can_send_other_messages",
                    "can_add_web_page_previews", "can_send_media_messages",
                )
                saved = {}
                for field in fields:
                    value = getattr(current, field, None)
                    if value is not None:
                        saved[field] = value
                settings["chat_permissions_before_lock"] = saved
            else:
                settings["chat_permissions_before_lock"] = {
                    "can_send_messages": True, "can_send_audios": True,
                    "can_send_documents": True, "can_send_photos": True,
                    "can_send_videos": True, "can_send_video_notes": True,
                    "can_send_voice_notes": True, "can_send_polls": True,
                    "can_send_other_messages": True, "can_add_web_page_previews": True,
                }
        await bot.set_chat_permissions(
            message.chat.id,
            permissions=ChatPermissions(
                can_send_messages=False,
                can_send_audios=False,
                can_send_documents=False,
                can_send_photos=False,
                can_send_videos=False,
                can_send_video_notes=False,
                can_send_voice_notes=False,
                can_send_polls=False,
                can_send_other_messages=False,
                can_add_web_page_previews=False,
            ),
        )
        settings["chat_locked_by_bot"] = True
        save_chat_settings(message.chat.id, settings)
        audit(message.chat.id, message.from_user.id, "chat_close")
        await message.answer("🔒 Чат закрыт. Обычные участники временно не могут отправлять сообщения. Открыть: <code>+чат</code>.")
    except Exception as exc:
        await safe_error(message, exc)


router.message(F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}), F.from_user)(auto_moderation_filters)


# ── Owner console + community utilities (increment 2) ────────────────────────
_PROCESS_STARTED = time.monotonic()
_RUNTIME_FLAGS: dict[str, Any] = {"maintenance": False}

OWNER_HELP = (
    "<b>🛠 Панель овнера Pear CM</b>\n\n"
    "<b>Диагностика</b>\n"
    "/status — статус и время работы\n/ping — проверка Telegram/Supabase\n"
    "/diag — конфигурация без вывода секретов\n/logs [число] — последние записи аудита\n"
    "/globalstats — статистика базы\n/exportstats — выгрузка статистики JSON\n"
    "/modules — состояние основных модулей\n/version — версия сборки\n"
    "/load — доступная нагрузочная информация\n/permissions — проверка прав бота в группе\n\n"
    "<b>Управление</b>\n/maintenance on|off|status — режим обслуживания\n"
    "/operator ID — выдать операторский доступ\n/revokeoperator ID — отозвать доступ\n"
    "/operators — список операторов\n/globalban ID причина — глобальный запрет в боте\n"
    "/globalunban ID — снять глобальный запрет\n/audit ID — аудит действий по ID\n\n"
    "<i>Овнерская панель доступна только BOT_OWNER_ID.</i>"
)

def _global_setting(key: str, default: Any = None) -> Any:
    row = db_one("bot_global_settings", key=key)
    if row is not None:
        return row.get("value", default)
    # Runtime fallback keeps simple settings available when the migration is absent.
    return _RUNTIME_FLAGS.get(key, default)

def _set_global_setting(key: str, value: Any) -> bool:
    _RUNTIME_FLAGS[key] = value
    row = db_upsert("bot_global_settings", {
        "key": key, "value": str(value),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }, "key")
    return row is not None

def _operator_active(user_id: int) -> bool:
    row = db_one("bot_operators", user_id=user_id, active=True)
    return bool(row and row.get("active"))

async def _owner_only(message: Message) -> bool:
    if message.from_user and message.from_user.id == OWNER_ID:
        return True
    await message.answer("⛔ Эта команда доступна только овнеру бота.")
    return False

async def _owner_or_operator(message: Message) -> bool:
    if message.from_user and (message.from_user.id == OWNER_ID or _operator_active(message.from_user.id)):
        return True
    await message.answer("⛔ Нужен доступ овнера или активного оператора.")
    return False

@router.message(Command("owner", "ownerpanel"))
@router.message(F.text.regexp(r"(?i)^\s*(?:овнер панель|панель овнера|админка овнера)\s*$"))
async def owner_panel(message: Message) -> None:
    if not await _owner_only(message):
        return
    await message.answer(OWNER_HELP, reply_markup=main_keyboard())

@router.message(Command("status", "статусбота"))
@router.message(F.text.regexp(r"(?i)^\s*статус бота\s*$"))
async def owner_status(message: Message) -> None:
    if not await _owner_or_operator(message):
        return
    try:
        me = await bot.get_me()
        telegram_state = f"OK (@{me.username or 'unknown'})"
    except Exception as exc:
        telegram_state = f"ошибка: {type(exc).__name__}"
    uptime = max(0, int(time.monotonic() - _PROCESS_STARTED))
    h, r = divmod(uptime, 3600); m, s = divmod(r, 60)
    users = db_rows("bot_users", limit=1000)
    await message.answer(
        "<b>📡 Статус Pear CM</b>\n"
        f"Telegram API: <code>{telegram_state}</code>\n"
        f"Процесс: <b>работает</b> · uptime <code>{h}ч {m}м {s}с</code>\n"
        f"База: <b>{'настроена' if db is not None else 'не настроена'}</b>\n"
        f"Записей пользователей в выборке: <b>{len(users)}</b>\n"
        f"Техобслуживание: <b>{'включено' if str(_global_setting('maintenance','off')).lower() == 'on' else 'выключено'}</b>\n"
        f"Московское время: <code>{format_moscow_time(moscow_now())}</code>"
    )

@router.message(Command("ping", "пинг"))
async def owner_ping(message: Message) -> None:
    if not await _owner_or_operator(message):
        return
    start = time.perf_counter()
    try:
        await bot.get_me()
        telegram = f"{(time.perf_counter()-start)*1000:.0f} мс"
    except Exception as exc:
        telegram = f"ошибка ({type(exc).__name__})"
    db_result = "не настроена"
    if db is not None:
        start = time.perf_counter()
        try:
            db.table("bot_users").select("user_id").limit(1).execute()
            db_result = f"OK · {(time.perf_counter()-start)*1000:.0f} мс"
        except Exception as exc:
            db_result = f"ошибка ({type(exc).__name__})"
    await message.answer(f"🏓 Telegram API: <b>{telegram}</b>\nSupabase: <b>{db_result}</b>")

@router.message(Command("diag", "diagnostics"))
@router.message(F.text.regexp(r"(?i)^\s*диагностика\s*$"))
async def owner_diagnostics(message: Message) -> None:
    if not await _owner_or_operator(message):
        return
    checks = [
        ("Python", sys.version.split()[0]),
        ("Платформа", platform.platform()),
        ("Часовой пояс", "Europe/Moscow (UTC+3)"),
        ("TELEGRAM_BOT_TOKEN задан", "да" if BOT_TOKEN else "нет"),
        ("SUPABASE_URL задан", "да" if SUPABASE_URL else "нет"),
        ("SUPABASE_SERVICE_KEY задан", "да" if SUPABASE_KEY else "нет"),
        ("BOT_OWNER_ID", str(OWNER_ID)),
    ]
    lines = [f"• {name}: <code>{val}</code>" for name, val in checks]
    if db is not None:
        for table in ("bot_users", "bot_audit", "bot_chat_settings", "bot_global_settings", "bot_operators", "bot_global_bans"):
            try:
                db.table(table).select("*").limit(1).execute()
                lines.append(f"• {table}: <b>OK</b>")
            except Exception:
                lines.append(f"• {table}: <b>нет доступа/таблица не создана</b>")
    await message.answer("<b>🧪 Диагностика</b>\n" + "\n".join(lines) + "\n\nСекретные значения не выводятся.")

@router.message(Command("globalstats", "глобальнаястатистика"))
@router.message(F.text.regexp(r"(?i)^\s*глобальная статистика\s*$"))
async def owner_global_stats(message: Message) -> None:
    if not await _owner_or_operator(message):
        return
    rows = db_rows("bot_users", limit=1000)
    audits = db_rows("bot_audit", limit=1000)
    chats = {r.get("chat_id") for r in rows if r.get("chat_id") is not None}
    messages = sum(int(r.get("messages") or 0) for r in rows)
    warns = sum(int(r.get("warns") or 0) for r in rows)
    await message.answer(
        "<b>🌐 Глобальная статистика</b>\n"
        f"Пользовательских записей (выборка до 1000): <b>{len(rows)}</b>\n"
        f"Чатов в выборке: <b>{len(chats)}</b>\n"
        f"Сообщений по счётчикам: <b>{messages}</b>\nПредупреждений по счётчикам: <b>{warns}</b>\n"
        f"Записей аудита в выборке: <b>{len(audits)}</b>\nВремя: <code>{format_moscow_time(moscow_now())}</code>"
    )

@router.message(Command("logs"))
@router.message(F.text.regexp(r"(?i)^\s*логи(?:\s+\d+)?\s*$"))
async def owner_logs(message: Message) -> None:
    if not await _owner_or_operator(message):
        return
    count = 10
    args = args_after_command(message)
    # Russian phrase has no slash command token; parse trailing integer directly.
    match = re.search(r"(?:логи|/logs)\s+(\d+)", message.text or "", re.I)
    if match:
        count = max(1, min(20, int(match.group(1))))
    rows = db_rows("bot_audit", limit=count, order="created_at")
    if not rows:
        await message.answer("Логи пока пусты или таблица bot_audit недоступна.")
        return
    lines=[]
    for row in rows:
        created = row.get("created_at")
        stamp = str(created or "время неизвестно")
        try:
            stamp = format_moscow_time(datetime.fromisoformat(stamp.replace("Z", "+00:00")))
        except Exception:
            pass
        detail = (row.get("details") or "—").replace("<", "&lt;").replace(">", "&gt;")[:150]
        lines.append(f"• <code>{stamp}</code> · <b>{row.get('action','?')}</b> · actor <code>{row.get('actor_id','?')}</code> · target <code>{row.get('target_id') or '—'}</code>\n  {detail}")
    await message.answer("<b>📜 Последние события</b>\n" + "\n".join(lines))

@router.message(Command("audit"))
@router.message(F.text.regexp(r"(?i)^\s*аудит\s+\d+\s*$"))
async def owner_audit_user(message: Message) -> None:
    if not await _owner_or_operator(message):
        return
    match = re.search(r"(?:/audit|аудит)\s+(\d+)", message.text or "", re.I)
    if not match:
        await message.answer("Формат: <code>/audit USER_ID</code> или <code>аудит USER_ID</code>")
        return
    uid=int(match.group(1))
    rows=db_rows("bot_audit", limit=20, order="created_at", target_id=uid)
    if not rows:
        await message.answer("По этому ID записей аудита не найдено.")
        return
    await message.answer("<b>Аудит ID " + str(uid) + "</b>\n" + "\n".join(
        f"• {r.get('action','?')} · {r.get('created_at','?')} · {(r.get('details') or '—')[:180]}" for r in rows
    ))

@router.message(Command("version", "версия"))
async def owner_version(message: Message) -> None:
    if not await _owner_or_operator(message): return
    await message.answer(f"Pear CM · сборка <code>2026.10-owner-console</code>\nPython <code>{sys.version.split()[0]}</code>\nМСК <code>{format_moscow_time(moscow_now())}</code>")

@router.message(Command("modules", "модули"))
async def owner_modules(message: Message) -> None:
    if not await _owner_or_operator(message): return
    modules = ["профили/активность", "модерация: mute/ban/unban/kick/warn", "аудит действий", "фильтры антиспам", "браки", "московское время", "панель овнера", "диагностика", "статистика"]
    await message.answer("<b>🧩 Модули основного iris.py</b>\n" + "\n".join("• " + m + " — загружен" for m in modules) + "\n\nОтдельный moderation_full.py не импортируется автоматически: у него legacy-зависимости.")

@router.message(Command("load", "нагрузка"))
async def owner_load(message: Message) -> None:
    if not await _owner_or_operator(message): return
    tasks = len(asyncio.all_tasks()) if hasattr(asyncio, "all_tasks") else -1
    uptime = int(time.monotonic() - _PROCESS_STARTED)
    await message.answer(f"<b>⚙️ Нагрузка процесса</b>\nТекущих asyncio-задач: <code>{tasks}</code>\nUptime: <code>{uptime} сек.</code>\nPID: <code>{os.getpid()}</code>\nCPU/RAM точные метрики недоступны без psutil.")

@router.message(Command("maintenance", "техобслуживание"))
@router.message(F.text.regexp(r"(?i)^\s*техобслуживание(?:\s+(?:on|off|статус|вкл|выкл))?\s*$"))
async def owner_maintenance(message: Message) -> None:
    if not await _owner_only(message): return
    raw=(message.text or "").lower().strip()
    if raw.startswith("/maintenance"):
        args=args_after_command(message); action=args[0].lower() if args else "status"
    else:
        parts=raw.split(); action=parts[1] if len(parts)>1 else "status"
    if action in {"on", "вкл", "включить"}:
        value="on"
    elif action in {"off", "выкл", "выключить"}:
        value="off"
    elif action in {"status", "статус"}:
        value=str(_global_setting("maintenance", "off")).lower()
    else:
        await message.answer("Формат: <code>/maintenance on</code> или <code>/maintenance off</code>.")
        return
    if action not in {"status", "статус"}:
        _set_global_setting("maintenance", value)
        audit(message.chat.id, message.from_user.id, "maintenance", None, value)
    await message.answer(f"🛠 Техобслуживание: <b>{'включено' if value == 'on' else 'выключено'}</b>.\n<i>Флаг сохраняется в Supabase, если таблица bot_global_settings создана; иначе действует до перезапуска.</i>")

@router.message(Command("operator"))
@router.message(F.text.regexp(r"(?i)^\s*выдать доступ\s+\d+\s*$"))
async def owner_add_operator(message: Message) -> None:
    if not await _owner_only(message): return
    match=re.search(r"(?:/operator|выдать доступ)\s+(\d+)", message.text or "", re.I)
    if not match:
        await message.answer("Формат: <code>/operator USER_ID</code> или <code>выдать доступ USER_ID</code>")
        return
    uid=int(match.group(1))
    if uid == OWNER_ID:
        await message.answer("Этот ID уже является овнером."); return
    row=db_upsert("bot_operators", {"user_id":uid,"active":True,"created_by":OWNER_ID,"created_at":datetime.now(timezone.utc).isoformat()}, "user_id")
    if row:
        audit(message.chat.id, OWNER_ID, "operator_add", uid, "operator granted")
        await message.answer(f"✅ Доступ оператора выдан ID <code>{uid}</code>.")
    else:
        await message.answer("Не удалось сохранить доступ. Создай таблицу bot_operators из schema.sql и проверь Supabase.")

@router.message(Command("revokeoperator"))
@router.message(F.text.regexp(r"(?i)^\s*отозвать доступ\s+\d+\s*$"))
async def owner_revoke_operator(message: Message) -> None:
    if not await _owner_only(message): return
    match=re.search(r"(?:/revokeoperator|отозвать доступ)\s+(\d+)", message.text or "", re.I)
    if not match:
        await message.answer("Формат: <code>/revokeoperator USER_ID</code> или <code>отозвать доступ USER_ID</code>"); return
    uid=int(match.group(1))
    row=db_upsert("bot_operators", {"user_id":uid,"active":False,"created_by":OWNER_ID,"created_at":datetime.now(timezone.utc).isoformat()}, "user_id")
    if row:
        audit(message.chat.id, OWNER_ID, "operator_revoke", uid, "operator revoked")
        await message.answer(f"🔒 Доступ ID <code>{uid}</code> отозван.")
    else: await message.answer("Не удалось обновить доступ. Проверь таблицу bot_operators в Supabase.")

@router.message(Command("operators", "операторы"))
async def owner_list_operators(message: Message) -> None:
    if not await _owner_only(message): return
    rows=db_rows("bot_operators", limit=100, order="created_at")
    if not rows:
        await message.answer("Список пуст или таблица bot_operators не создана."); return
    await message.answer("<b>Операторы</b>\n" + "\n".join(f"• <code>{r.get('user_id')}</code> — {'активен' if r.get('active') else 'отозван'}" for r in rows))

@router.message(Command("globalban"))
@router.message(F.text.regexp(r"(?i)^\s*глобальный бан\s+\d+(?:\s+.+)?$"))
async def owner_global_ban(message: Message) -> None:
    if not await _owner_only(message): return
    match=re.match(r"(?is)^\s*(?:/globalban|глобальный бан)\s+(\d+)(?:\s+(.*))?$", message.text or "")
    if not match:
        await message.answer("Формат: <code>/globalban USER_ID причина</code>"); return
    uid=int(match.group(1)); reason=(match.group(2) or "причина не указана")[:500]
    if uid == OWNER_ID:
        await message.answer("Нельзя заблокировать овнера."); return
    row=db_upsert("bot_global_bans", {"user_id":uid,"active":True,"reason":reason,"actor_id":OWNER_ID,"created_at":datetime.now(timezone.utc).isoformat()}, "user_id")
    if row:
        audit(message.chat.id, OWNER_ID, "globalban", uid, reason)
        await message.answer(f"🚫 Глобальный запрет включён для ID <code>{uid}</code>. Это блокировка внутри бота, не Telegram-wide бан.")
    else: await message.answer("Не удалось сохранить: создай таблицу bot_global_bans из schema.sql.")

@router.message(Command("globalunban"))
@router.message(F.text.regexp(r"(?i)^\s*глобальный разбан\s+\d+\s*$"))
async def owner_global_unban(message: Message) -> None:
    if not await _owner_only(message): return
    match=re.search(r"(?:/globalunban|глобальный разбан)\s+(\d+)", message.text or "", re.I)
    if not match:
        await message.answer("Формат: <code>/globalunban USER_ID</code>"); return
    uid=int(match.group(1))
    row=db_upsert("bot_global_bans", {"user_id":uid,"active":False,"reason":"","actor_id":OWNER_ID,"created_at":datetime.now(timezone.utc).isoformat()}, "user_id")
    if row:
        audit(message.chat.id, OWNER_ID, "globalunban", uid, "global ban removed")
        await message.answer(f"✅ Глобальный запрет для ID <code>{uid}</code> снят.")
    else: await message.answer("Не удалось сохранить: создай таблицу bot_global_bans из schema.sql.")

@router.message(Command("exportstats", "экспортстатистики"))
@router.message(F.text.regexp(r"(?i)^\s*экспорт статистики\s*$"))
async def owner_export_stats(message: Message) -> None:
    if not await _owner_only(message): return
    rows=db_rows("bot_users", limit=1000)
    data={"generated_at_moscow":format_moscow_time(moscow_now()),"sample_limit":1000,"rows":rows}
    raw=json.dumps(data, ensure_ascii=False, indent=2, default=str).encode("utf-8")
    await message.answer_document(BufferedInputFile(raw, filename="pear_cm_stats.json"), caption="Экспорт статистики (до 1000 записей; секреты не включены).")

@router.message(Command("permissions", "проверкаправ"))
@router.message(F.text.regexp(r"(?i)^\s*проверка прав\s*$"))
async def owner_permissions(message: Message) -> None:
    if not is_group(message):
        await message.answer("Запусти команду в группе, чтобы проверить права бота."); return
    try:
        me=await bot.get_me(); member=await bot.get_chat_member(message.chat.id, me.id)
        if member.status not in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR):
            await message.answer("⚠️ Бот не администратор группы. Мут/бан/удаление/закрепление могут не работать."); return
        flags=[]
        for attr,label in (("can_restrict_members","бан/мут"),("can_delete_messages","удаление"),("can_pin_messages","закрепление"),("can_invite_users","приглашения"),("can_manage_chat","управление чатом")):
            flags.append(f"• {label}: {'✅' if getattr(member,attr,False) else '❌'}")
        await message.answer("<b>Права бота</b>\n"+"\n".join(flags))
    except Exception as exc: await message.answer(f"Не удалось проверить права: <code>{type(exc).__name__}</code>")

@router.message(Command("calc", "калькулятор"))
@router.message(F.text.regexp(r"(?i)^\s*калькулятор\s+.+$"))
async def calculator_cmd(message: Message) -> None:
    raw=(message.text or "").strip()
    expr=re.sub(r"^(?:/calc(?:@\w+)?|/калькулятор|калькулятор)\s*", "", raw, flags=re.I).strip()
    if not expr:
        await message.answer("Формат: <code>/calc (12+8)*3</code>"); return
    try:
        tree=ast.parse(expr, mode="eval")
        allowed=(ast.Expression,ast.BinOp,ast.UnaryOp,ast.Constant,ast.Add,ast.Sub,ast.Mult,ast.Div,ast.FloorDiv,ast.Mod,ast.Pow,ast.USub,ast.UAdd,ast.Load)
        if any(not isinstance(n,allowed) for n in ast.walk(tree)): raise ValueError
        for n in ast.walk(tree):
            if isinstance(n,ast.Constant) and (not isinstance(n.value,(int,float)) or abs(n.value)>10**12): raise ValueError
            if isinstance(n,ast.Pow) and isinstance(n,ast.Constant) and abs(n.value)>10: raise ValueError
        result=eval(compile(tree,"<calc>","eval"),{"__builtins__":{}},{})
        if isinstance(result,float) and (result != result or abs(result)==float("inf")): raise ValueError
        await message.answer(f"🧮 <code>{expr}</code> = <b>{result}</b>")
    except Exception: await message.answer("Не получилось вычислить. Разрешены числа и операции + - * / // % **, без переменных и функций.")

@router.message(Command("random", "рандом"))
@router.message(F.text.regexp(r"(?i)^\s*рандом(?:\s+\d+\s+\d+)?\s*$"))
async def random_number_cmd(message: Message) -> None:
    raw=(message.text or "").strip()
    match=re.search(r"(?:/random|/рандом|рандом)\s+(\d+)\s+(\d+)",raw,re.I)
    lo,hi=(int(match.group(1)),int(match.group(2))) if match else (1,100)
    if lo>hi or hi-lo>10**9:
        await message.answer("Укажи корректный диапазон, например <code>/random 1 100</code>."); return
    await message.answer(f"🎲 Случайное число: <b>{random.randint(lo,hi)}</b>")

@router.message(Command("dice", "кубик"))
@router.message(F.text.regexp(r"(?i)^\s*кубик\s*$"))
async def dice_cmd(message: Message) -> None:
    await message.answer_dice(emoji="🎲")

@router.message(Command("date", "дата"))
@router.message(F.text.regexp(r"(?i)^\s*дата\s*$"))
async def date_cmd(message: Message) -> None:
    now=moscow_now()
    await message.answer(f"🕒 <b>{format_moscow_time(now)}</b>\nЧасовой пояс: <code>Europe/Moscow (UTC+3)</code>")

@router.message(Command("activity", "актив"))
@router.message(F.text.regexp(r"(?i)^\s*актив\s*$"))
async def activity_cmd(message: Message) -> None:
    rows=db_rows("bot_users",limit=10,order="messages",chat_id=message.chat.id)
    if not rows:
        await message.answer("Пока нет статистики активности в этой группе."); return
    lines=[]
    for idx,row in enumerate(rows,1):
        name=(row.get("first_name") or row.get("username") or str(row.get("user_id"))).replace("<","&lt;").replace(">","&gt;")
        lines.append(f"{idx}. {name} — <b>{int(row.get('messages') or 0)}</b> сообщений")
    await message.answer("<b>📈 Активные участники (сохранённые счётчики)</b>\n"+"\n".join(lines))

@router.message(Command("suggest", "предложение"))
@router.message(F.text.regexp(r"(?i)^\s*предложение\s+.+$"))
async def suggestion_cmd(message: Message) -> None:
    if not message.from_user:
        return
    raw=(message.text or "")
    text_value=re.sub(r"^(?:/suggest(?:@\w+)?|/предложение|предложение)\s*", "", raw, flags=re.I).strip()
    if not text_value:
        await message.answer("Формат: <code>/suggest идея</code>"); return
    row=db_insert("bot_reports", {"chat_id":message.chat.id,"reporter_id":message.from_user.id,"message_id":None,"details":"ПРЕДЛОЖЕНИЕ: "+text_value[:1400],"status":"suggestion","created_at":datetime.now(timezone.utc).isoformat()})
    await message.answer("💡 Предложение сохранено." if row else "Таблица bot_reports недоступна. Примените schema.sql.")

@router.message(Command("quote", "цитата"))
@router.message(F.text.regexp(r"(?i)^\s*цитата\s*$"))
async def quote_cmd(message: Message) -> None:
    if not message.reply_to_message:
        await message.answer("Ответь на сообщение командой <code>/quote</code> или <code>цитата</code>."); return
    src=message.reply_to_message
    text_value=(src.text or src.caption or "[сообщение без текста]")[:2500]
    author=src.from_user.full_name if src.from_user else "неизвестный автор"
    await message.answer(f"<blockquote>{text_value.replace('&','&amp;').replace('<','&lt;').replace('>','&gt;')}</blockquote>\n— {author}\n<code>{format_moscow_time(moscow_now())}</code>")

@router.message(Command("favorites", "избранное"))
@router.message(F.text.regexp(r"(?i)^\s*избранное\s*$"))
async def favorites_cmd(message: Message) -> None:
    if not message.from_user:
        return
    if not message.reply_to_message:
        await message.answer("Ответь на сообщение командой <code>/favorites</code>, чтобы сохранить его в избранное."); return
    src=message.reply_to_message
    text_value=(src.text or src.caption or f"message_id={src.message_id}")[:2500]
    row=db_insert("bot_notes", {"user_id":message.from_user.id,"content":f"ИЗБРАННОЕ · chat={src.chat.id} · message={src.message_id}\n{text_value}","created_at":datetime.now(timezone.utc).isoformat()})
    await message.answer("⭐ Сообщение сохранено в личные заметки." if row else "Не удалось сохранить: проверь таблицу bot_notes из schema.sql.")

@router.message(Command("achievements", "достижения"))
@router.message(F.text.regexp(r"(?i)^\s*достижения\s*$"))
async def achievements_cmd(message: Message) -> None:
    if not message.from_user:
        return
    row=db_one("bot_users",user_id=message.from_user.id,chat_id=message.chat.id) or {}
    msgs=int(row.get("messages") or 0); warns=int(row.get("warns") or 0)
    badges=[]
    if msgs>=1: badges.append("🌱 Первое сообщение")
    if msgs>=100: badges.append("💬 100 сообщений")
    if msgs>=1000: badges.append("🔥 1000 сообщений")
    if warns==0 and msgs>=50: badges.append("🛡 Аккуратный участник")
    await message.answer("<b>🏅 Достижения</b>\n"+("\n".join(badges) if badges else "Пока нет достижений — участвуй в общении!"))

@router.message(Command("language", "язык"))
@router.message(F.text.regexp(r"(?i)^\s*язык\s*$"))
async def language_cmd(message: Message) -> None:
    await message.answer("🌐 Сейчас интерфейс Pear CM работает на русском. Переключение языка будет доступно после добавления сохранения пользовательских предпочтений.")

@router.message(Command("notifications", "уведомления"))
@router.message(F.text.regexp(r"(?i)^\s*уведомления\s*$"))
async def notifications_cmd(message: Message) -> None:
    await message.answer("🔔 Личные уведомления пока не настраиваются отдельно. Команда зарезервирована, настройки можно добавить в следующем этапе.")

@router.message(Command("online", "ктоонлайн"))
@router.message(F.text.regexp(r"(?i)^\s*кто онлайн\s*$"))
async def online_cmd(message: Message) -> None:
    rows=db_rows("bot_users",limit=20,order="last_seen",chat_id=message.chat.id)
    if not rows:
        await message.answer("Нет сохранённой активности."); return
    lines=[]
    for row in rows:
        name=(row.get("first_name") or row.get("username") or str(row.get("user_id"))).replace("<","&lt;").replace(">","&gt;")
        stamp=row.get("last_seen") or "неизвестно"
        lines.append(f"• {name} — последнее сообщение: <code>{stamp}</code>")
    await message.answer("<b>🟢 Недавно активные (это не список онлайн-статуса Telegram)</b>\n"+"\n".join(lines))



# ── Global error handling / startup ──────────────────────────────────────────
@dp.errors()
async def global_error_handler(event) -> bool:
    log.error("Unhandled update error: %r", getattr(event, "exception", event))
    return True

async def main() -> None:
    # Меню команд разделено по контекстам: в группах — только управление чатом,
    # обычным пользователям в личке — пользовательские функции, овнерские команды —
    # только в личном чате владельца бота.
    group_commands = [
        BotCommand(command="start", description="Запустить бота"),
        BotCommand(command="help", description="Справка по командам чата"),
        BotCommand(command="admins", description="Администраторы чата"),
        BotCommand(command="warns", description="Мои предупреждения"),
        BotCommand(command="top", description="Топ участников"),
        BotCommand(command="mute", description="Мут участника (ответом)"),
        BotCommand(command="unmute", description="Снять мут (ответом)"),
        BotCommand(command="ban", description="Забанить (ответом)"),
        BotCommand(command="kick", description="Исключить (ответом)"),
        BotCommand(command="warn", description="Предупреждение (ответом)"),
        BotCommand(command="pin", description="Закрепить сообщение (ответом)"),
        BotCommand(command="del", description="Удалить сообщение (ответом)"),
        BotCommand(command="purge", description="Очистить сообщения (ответом)"),
        BotCommand(command="openchat", description="Открыть чат"),
        BotCommand(command="closechat", description="Закрыть чат"),
        BotCommand(command="antiflood", description="Настроить антифлуд"),
        BotCommand(command="antispam", description="Настроить антиспам"),
        BotCommand(command="antiraid", description="Настроить антирейд"),
        BotCommand(command="checkrights", description="Проверить права бота"),
        BotCommand(command="history", description="История наказаний"),
        BotCommand(command="report", description="Отправить жалобу"),
        BotCommand(command="appeal", description="Апелляция"),
        BotCommand(command="calc", description="Калькулятор"),
        BotCommand(command="random", description="Случайное число"),
        BotCommand(command="dice", description="Бросить кубик"),
    ]
    private_commands = [
        BotCommand(command="start", description="Открыть меню бота"),
        BotCommand(command="help", description="Справка"),
        BotCommand(command="profile", description="Мой профиль"),
        BotCommand(command="warns", description="Мои предупреждения"),
        BotCommand(command="top", description="Топ участников"),
        BotCommand(command="calc", description="Калькулятор"),
        BotCommand(command="random", description="Случайное число"),
        BotCommand(command="dice", description="Бросить кубик"),
        BotCommand(command="date", description="Дата и московское время"),
        BotCommand(command="activity", description="Активность"),
        BotCommand(command="suggest", description="Предложить идею"),
        BotCommand(command="quote", description="Цитата из ответа"),
        BotCommand(command="favorites", description="Сохранить в избранное"),
        BotCommand(command="achievements", description="Достижения"),
        BotCommand(command="language", description="Язык интерфейса"),
        BotCommand(command="notifications", description="Уведомления"),
        BotCommand(command="online", description="Недавно активные"),
        BotCommand(command="report", description="Отправить жалобу"),
        BotCommand(command="appeal", description="Апелляция"),
    ]
    owner_commands = [
        BotCommand(command="owner", description="Панель овнера"),
        BotCommand(command="status", description="Статус процесса"),
        BotCommand(command="ping", description="Проверка Telegram и БД"),
        BotCommand(command="diag", description="Диагностика конфигурации"),
        BotCommand(command="globalstats", description="Глобальная статистика"),
        BotCommand(command="modules", description="Состояние модулей"),
        BotCommand(command="version", description="Версия бота"),
        BotCommand(command="logs", description="Последние записи аудита"),
        BotCommand(command="maintenance", description="Режим техобслуживания"),
        BotCommand(command="operator", description="Выдать доступ оператору"),
        BotCommand(command="revokeoperator", description="Отозвать доступ оператора"),
        BotCommand(command="operators", description="Список операторов"),
        BotCommand(command="globalban", description="Глобальный запрет в боте"),
        BotCommand(command="globalunban", description="Снять глобальный запрет"),
        BotCommand(command="exportstats", description="Экспорт статистики JSON"),
        BotCommand(command="permissions", description="Проверить права бота"),
        BotCommand(command="auditlog", description="Аудит действий"),
        BotCommand(command="reports", description="Очередь жалоб"),
        BotCommand(command="appeals", description="Очередь апелляций"),
        BotCommand(command="closereport", description="Закрыть жалобу"),
        BotCommand(command="closeappeal", description="Закрыть апелляцию"),
    ]
    await bot.set_my_commands(group_commands, scope=BotCommandScopeAllGroupChats())
    await bot.set_my_commands(private_commands, scope=BotCommandScopeAllPrivateChats())
    await bot.set_my_commands(owner_commands, scope=BotCommandScopeChat(chat_id=OWNER_ID))
    await bot.delete_webhook(drop_pending_updates=False)
    log.info("Bot started")
    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        await bot.session.close()

if __name__ == "__main__":
    asyncio.run(main())
