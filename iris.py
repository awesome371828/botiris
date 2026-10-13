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
    "antibot": False, "antisticker": False, "antigif": False,
    "antiphoto": False, "antivideo": False, "antivoice": False,
    "antidocument": False, "flood_limit": 5, "flood_window": 10,
    "caps_percent": 70, "filter_mute_minutes": 10,
    "warn_3_action": "mute", "warn_3_time": "1ч",
    "warn_5_action": "mute", "warn_5_time": "24ч",
    "warn_7_action": "ban", "warn_7_time": "7д",
    "warn_10_action": "ban", "warn_10_time": "навсегда",
}
_CHAT_SETTINGS_CACHE: dict[int, dict[str, Any]] = {}
_FLOOD_TIMES: dict[tuple[int, int], deque[float]] = defaultdict(deque)
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
        "• /time — текущее московское время (Europe/Moscow, UTC+3)\n\n"
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
    # Never filter commands; moderation/admin commands must remain reachable.
    text_value = message.text or message.caption or ""
    if text_value and re.match(r"^\s*(?:[/!.+-]|мут(?:\s|$)|бан(?:\s|$)|кик(?:\s|$)|варн(?:\s|$)|размут(?:\s|$))", text_value, re.I):
        return
    if await is_admin(bot, message.chat.id, message.from_user.id) or user_rank(message.chat.id, message.from_user.id) >= 1:
        return
    settings = get_chat_settings(message.chat.id)
    reason = None
    if settings.get("antibot") and message.new_chat_members and any(u.is_bot for u in message.new_chat_members):
        reason = "добавление бота"
    if text_value:
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
router.message(F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}), F.from_user)(auto_moderation_filters)

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
        BotCommand(command="time", description="Текущее московское время"),
        BotCommand(command="top", description="Топ участников"),
        BotCommand(command="mute", description="Мут: срок и причина (ответ)"),
        BotCommand(command="rank", description="Ранг модератора"),
        BotCommand(command="settrigger", description="Настроить триггеры варнов"),
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
