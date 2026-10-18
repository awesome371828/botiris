"""
Pear / Iris-style Telegram group manager — independent implementation.
Python 3.11+, aiogram 3, Supabase.
Set TELEGRAM_BOT_TOKEN, SUPABASE_URL and SUPABASE_SERVICE_KEY in environment.
"""
from __future__ import annotations

import asyncio
import ast
import difflib
import json
import platform
import random
import sys
from io import BytesIO
from html import escape
import logging
import os
import re
import time
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from typing import Any

from aiogram import Bot, Dispatcher, F, Router, BaseMiddleware
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatMemberStatus, ChatType, ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramConflictError, TelegramForbiddenError, TelegramRetryAfter
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    BotCommand, BotCommandScopeAllGroupChats, BotCommandScopeAllPrivateChats,
    BotCommandScopeChat, ChatPermissions, KeyboardButton, Message, CallbackQuery, BufferedInputFile,
    ReplyKeyboardMarkup, ReplyKeyboardRemove, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo, MenuButtonWebApp,
)
from supabase import Client, create_client

try:
    import fcntl
except ImportError:  # pragma: no cover - flock is available on the Linux bot runner
    fcntl = None

_PROCESS_LOCK_HANDLE = None

def acquire_single_instance_lock() -> None:
    """Prevent two local polling processes from using the same bot token."""
    global _PROCESS_LOCK_HANDLE
    if fcntl is None:
        log.warning("Process lock unavailable on this platform; rely on deployment concurrency controls")
        return
    lock_path = os.getenv("BOT_LOCK_PATH", "/tmp/pear_cm_polling.lock")
    handle = open(lock_path, "w", encoding="utf-8")
    # Wait for the current local instance to exit rather than repeatedly restarting
    # and hammering getUpdates. flock releases automatically when the process dies.
    fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
    _PROCESS_LOCK_HANDLE = handle  # Keep the descriptor open for the entire process lifetime.
    log.info("Single-instance polling lock acquired: %s", lock_path)


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
MINI_APP_URL = "https://awesome371828.github.io/botiris/?v=20261014-6"

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

bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML, link_preview_is_disabled=True))
dp = Dispatcher()
router = Router()
dp.include_router(router)

# Register participant-call aliases before all feature handlers. aiogram stops at
# the first matching handler, so this must precede broad text/command handlers.
@router.message(Command("sbor"))
@router.message(F.text.regexp(r"(?i)^\s*/sbor(?:@pear_cm_bot)?(?:\s|$)"))
@router.message(F.text.regexp(r"(?i)^\s*(?:калл|колл|call|сбор|общий сбор|вызов участников)(?:\s+.*|$)"))
async def participant_call_dispatch_first(message: Message) -> None:
    await gather_cmd(message)

@router.message(F.text.regexp(r"(?i)^\s*анрег\s*$"))
async def participant_call_unregister_dispatch_first(message: Message) -> None:
    await participant_call_unregister(message)

# In private chats, /ban and /unban are global bot restrictions for the owner/operators.
# In groups, the existing moderation handlers retain their normal chat-ban semantics.
@router.message(Command("ban"), F.chat.type == ChatType.PRIVATE)
async def private_global_ban_command(message: Message) -> None:
    if not await _owner_or_operator(message):
        return
    await _global_moderation_command(message, remove=False)

@router.message(Command("unban"), F.chat.type == ChatType.PRIVATE)
async def private_global_unban_command(message: Message) -> None:
    if not await _owner_or_operator(message):
        return
    await _global_moderation_command(message, remove=True)

# Rate-limit only rapid repeats of the SAME command. Different commands remain usable back-to-back.
_COMMAND_LATENCIES: deque[tuple[float, str]] = deque(maxlen=2000)
_COMMAND_LAST: dict[tuple[int, int, str], float] = {}
_COMMAND_NOTICE_LAST: dict[tuple[int, int, str], float] = {}

class CommandRateLimitMiddleware(BaseMiddleware):
    async def __call__(self, handler, event, data):
        if not isinstance(event, Message) or not event.from_user or event.from_user.is_bot:
            return await handler(event, data)
        # Enforce the bot's global-ban table before any command/feature handler runs.
        # The helper has a TTL cache; run its cold Supabase lookup off the event loop.
        if event.from_user.id != OWNER_ID and await asyncio.to_thread(_is_globally_banned, event.from_user.id):
            now = time.monotonic()
            notice_at = _GLOBAL_BAN_NOTICE_LAST.get(event.from_user.id, 0.0)
            if event.chat.type == ChatType.PRIVATE and now - notice_at >= 10:
                _GLOBAL_BAN_NOTICE_LAST[event.from_user.id] = now
                try:
                    await event.answer("⛔ Доступ к функциям Pear CM для этого аккаунта ограничен.")
                except Exception:
                    pass
            return None
        text_value = (event.text or "").strip()
        command_match = re.match(r"^([/!.][\w]+)(?:@\w+)?", text_value, re.I)
        plain_match = re.match(
            r"^(мут|бан|кик|варн|размут|анмут|разбан|очистить|профиль|команды|помощь)(?:\s|$)",
            text_value, re.I,
        )
        if not command_match and not plain_match:
            return await handler(event, data)
        command_name = (command_match.group(1) if command_match else plain_match.group(1)).lower()
        # Keep help/start responsive in groups even after repeated taps.
        if command_name in {"/help", "/commands", "/start", "/помощь", "/команды"}:
            return await handler(event, data)
        key = (event.chat.id, event.from_user.id, command_name)
        now = time.monotonic()
        previous = _COMMAND_LAST.get(key, 0.0)
        if now - previous < 0.8:
            last_notice = _COMMAND_NOTICE_LAST.get(key, 0.0)
            if now - last_notice >= 3.0:
                _COMMAND_NOTICE_LAST[key] = now
                try:
                    await event.answer("⏳ Повтори эту же команду чуть позже.")
                except Exception:
                    pass
            return None
        _COMMAND_LAST[key] = now
        if len(_COMMAND_LAST) > 10000:
            cutoff = now - 3600
            for old_key, stamp in list(_COMMAND_LAST.items()):
                if stamp < cutoff:
                    _COMMAND_LAST.pop(old_key, None)
                    _COMMAND_NOTICE_LAST.pop(old_key, None)
        return await handler(event, data)

router.message.middleware(CommandRateLimitMiddleware())

# Track real message IDs observed during this process. Telegram Bot API has no history endpoint.
_RECENT_MESSAGE_IDS: dict[int, deque[int]] = defaultdict(lambda: deque(maxlen=5000))

async def _remember_message_id(handler, event: Message, data):
    started = time.monotonic()
    if isinstance(event, Message):
        _RECENT_MESSAGE_IDS[event.chat.id].append(event.message_id)
    raw = (event.text or event.caption or "").strip() if isinstance(event, Message) else ""
    # Owner audit trail: record every slash command and the important text aliases.
    # Keep logging asynchronous so ordinary chat commands remain responsive.
    if isinstance(event, Message) and event.from_user and raw and (
        raw.startswith(("/", "!", ".")) or re.match(
            r"(?i)^(калл|колл|call|сбор|общий сбор|вызов участников|мут|бан|кик|варн|размут|анмут|разбан|очистить|профиль|команды|помощь)(?:\\s|$)",
            raw,
        )
    ):
        try:
            asyncio.get_running_loop().create_task(asyncio.to_thread(
                db_insert, "bot_audit", _audit_payload(
                    event.chat.id, event.from_user.id, "command_received",
                    details=f"command={raw[:500]!r}; chat_type={event.chat.type}; message_id={event.message_id}",
                )
            ))
        except Exception:
            log.exception("Could not queue command audit event")
    try:
        return await handler(event, data)
    except Exception as exc:
        if isinstance(event, Message) and event.from_user and raw:
            log.exception("Unhandled handler error chat=%s user=%s text=%r", event.chat.id, event.from_user.id, raw[:300])
            try:
                asyncio.get_running_loop().create_task(asyncio.to_thread(
                    db_insert, "bot_audit", _audit_payload(
                        event.chat.id, event.from_user.id, "command_exception",
                        details=f"command={raw[:300]!r}; error={type(exc).__name__}: {str(exc)[:1200]}",
                    )
                ))
            except Exception:
                log.exception("Could not queue command exception audit")
        raise
    finally:
        elapsed = time.monotonic() - started
        _COMMAND_LATENCIES.append((elapsed, type(event).__name__))
        if elapsed >= 2.0:
            raw_text = (getattr(event, "text", None) or getattr(event, "caption", None) or "").replace("\n", " ")[:100]
            log.warning(
                "Slow update handler: %.3fs type=%s chat=%s user=%s message=%s text=%r",
                elapsed, type(event).__name__,
                getattr(getattr(event, "chat", None), "id", "?"),
                getattr(getattr(event, "from_user", None), "id", "?"),
                getattr(event, "message_id", "?"), raw_text,
            )
        if len(_RECENT_MESSAGE_IDS) > 1000:
            for old_chat in list(_RECENT_MESSAGE_IDS.keys())[:200]:
                _RECENT_MESSAGE_IDS.pop(old_chat, None)

router.message.outer_middleware(_remember_message_id)

# Runtime cache is used if Supabase is temporarily unavailable. Settings are also
# persisted in bot_chat_settings when the table from schema.sql exists.
FILTER_DEFAULTS = {
    "antilink": False, "antiinvite": False, "antimat": False,
    "antiflood": False, "anticaps": False, "antireklama": False,
    "antikripta": False, "antifishing": False, "antiall": False,
    "antibot": False, "antisticker": False, "antigif": False, "antispam": False, "antiraid": False,
    "antiphoto": False, "antivideo": False, "antivoice": False,
    "antidocument": False, "filter_action": "monitor", "flood_limit": 8, "flood_window": 10,
    "caps_percent": 70, "filter_mute_minutes": 10, "raid_threshold": 8, "raid_window": 30, "raid_restrict_minutes": 2,
    "warn_3_action": "ban", "warn_3_time": "7д",
    "warn_5_action": "ban", "warn_5_time": "7д",
    "warn_7_action": "ban", "warn_7_time": "7д",
    "warn_10_action": "ban", "warn_10_time": "навсегда",
}
_CHAT_SETTINGS_CACHE: dict[int, dict[str, Any]] = {}
_CHAT_SETTINGS_CACHE_TIME: dict[int, float] = {}
_CHAT_SETTINGS_CACHE_TTL = 5.0
_FLOOD_TIMES: dict[tuple[int, int], deque[float]] = defaultdict(deque)
_SPAM_LAST: dict[tuple[int, int], tuple[str, float]] = {}
_SPAM_HISTORY: dict[tuple[int, int], deque[tuple[float, str, int]]] = defaultdict(lambda: deque(maxlen=30))
_RAID_JOINS: dict[int, deque[float]] = defaultdict(deque)
_RAID_RESTRICTED: dict[tuple[int, int], float] = {}
BAD_WORDS = ("хуй", "пизд", "бляд", "ебал", "ебат", "сука", "мраз", "fuck", "shit", "bitch", "asshole")
AD_WORDS = ("купить", "продам", "продаю", "скидка", "акция", "распродажа", "заработок", "инвестиции", "крипта", "bitcoin", "ethereum", "usdt", "казино", "ставки", "букмекер", "промокод", "реферал", "подпишись")
FISHING_RE = re.compile(r"(?:bit\.ly|tinyurl|goo\.gl|t\.me/joinchat|verify.{0,20}wallet|seed.{0,20}phrase)", re.I)
URL_RE = re.compile(r"(?:https?://|www\.|t\.me/|telegram\.me/)", re.I)
INVITE_RE = re.compile(r"(?:t\.me/\+|t\.me/joinchat|telegram\.me/joinchat)", re.I)

# ── Keyboards ────────────────────────────────────────────────────────────────
def main_keyboard(
    user_id: int | None = None,
    chat_type: ChatType = ChatType.PRIVATE,
    privileged: bool = False,
) -> InlineKeyboardMarkup:
    # Inline navigation only. In groups, never expose owner/operator controls.
    return welcome_inline_keyboard(user_id, chat_type, privileged)

def welcome_inline_keyboard(
    user_id: int | None = None,
    chat_type: ChatType = ChatType.PRIVATE,
    privileged: bool = False,
) -> InlineKeyboardMarkup:
    # The owner panel is private-only and never shown to operators or groups.
    if chat_type != ChatType.PRIVATE:
        return InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📚 Команды", callback_data="home:help"),
             InlineKeyboardButton(text="🛡 Кто админ", callback_data="menu:admins")],
            [InlineKeyboardButton(text="💍 Брак", callback_data="menu:marriage"),
             InlineKeyboardButton(text="⚠️ Мои варны", callback_data="menu:warns")],
        ])

    rows = [
        [InlineKeyboardButton(text="➕ Добавить в чат", url="https://t.me/pear_cm_bot?startgroup=true")],
        [InlineKeyboardButton(text="📚 Команды", callback_data="home:help"),
         InlineKeyboardButton(text="💠 Профиль", callback_data="home:profile")],
        [InlineKeyboardButton(text="💍 Брак", callback_data="menu:marriage"),
         InlineKeyboardButton(text="⚠️ Мои варны", callback_data="menu:warns")],
        [InlineKeyboardButton(text="🛠 Установка", callback_data="menu:installation"),
         InlineKeyboardButton(text="🌐 Открыть приложение", url=MINI_APP_URL)],
    ]
    if user_id == OWNER_ID or privileged:
        rows.insert(1, [InlineKeyboardButton(text="🛠 Админ-панель", callback_data="admin:open")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def admin_inline_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🚫 Бан пользователя", callback_data="admin:ban"),
         InlineKeyboardButton(text="♻️ Разбан", callback_data="admin:unban")],
        [InlineKeyboardButton(text="👮 Операторы", callback_data="admin:operators"),
         InlineKeyboardButton(text="📊 Статистика", callback_data="admin:stats")],
        [InlineKeyboardButton(text="📡 Статус", callback_data="admin:status"),
         InlineKeyboardButton(text="🧪 Диагностика", callback_data="admin:diag")],
        [InlineKeyboardButton(text="📜 Логи", callback_data="admin:logs"),
         InlineKeyboardButton(text="📈 Общая статистика", callback_data="admin:globalstats")],
        [InlineKeyboardButton(text="🧩 Модули", callback_data="admin:modules"),
         InlineKeyboardButton(text="👥 Список операторов", callback_data="admin:operators_list")],
        [InlineKeyboardButton(text="🛠 Обслуживание", callback_data="admin:maintenance"),
         InlineKeyboardButton(text="🧰 Фильтры", callback_data="admin:filters")],
        [InlineKeyboardButton(text="⬅️ Главное меню", callback_data="admin:home")],
    ])

def profile_link(user_id: int, name: str) -> str:
    """Clickable Telegram profile link without creating a mention notification."""
    safe = escape(str(name or "пользователь"))
    if int(user_id or 0) > 0:
        return f'<a href="tg://user?id={int(user_id)}">{safe}</a>'
    return safe

def mention(user_id: int, name: str) -> str:
    """Clickable profile link intended for commands that explicitly ping users."""
    return profile_link(user_id, name)

# ── Database helpers ─────────────────────────────────────────────────────────
def _is_transient_db_error(exc: Exception) -> bool:
    """Identify temporary network/server failures; schema errors should fail immediately."""
    value = f"{type(exc).__name__} {exc}".casefold()
    return any(token in value for token in (
        "timeout", "timed out", "connection", "temporarily unavailable",
        "temporary failure", "network", "429", "502", "503", "504",
        "server disconnected", "rate limit",
    ))

def _db_read_with_retry(table: str, operation: str, build_query):
    """Retry idempotent Supabase reads/upserts on transient failures with bounded backoff."""
    for attempt in range(3):
        try:
            return build_query()
        except Exception as exc:
            if attempt < 2 and _is_transient_db_error(exc):
                # Never sleep/retry synchronously on the asyncio event-loop thread.
                try:
                    asyncio.get_running_loop()
                    on_event_loop = True
                except RuntimeError:
                    on_event_loop = False
                if on_event_loop:
                    log.warning("Temporary Supabase %s failure for %s on event loop; skipping blocking retry: %s",
                                operation, table, exc)
                    return None
                delay = 0.25 * (2 ** attempt)
                log.warning("Temporary Supabase %s failure for %s (attempt %s/3); retrying in %.2fs: %s",
                            operation, table, attempt + 1, delay, exc)
                time.sleep(delay)
                continue
            log.exception("Supabase %s failed: %s", operation, table)
            return None
    return None

def db_one(table: str, **filters: Any) -> dict | None:
    if db is None:
        return None
    def query():
        q = db.table(table).select("*")
        for key, value in filters.items():
            q = q.eq(key, value)
        rows = q.limit(1).execute().data or []
        return rows[0] if rows else None
    return _db_read_with_retry(table, "select", query)

def db_rows(table: str, limit: int = 20, order: str | None = None, **filters: Any) -> list[dict]:
    if db is None:
        return []
    def query():
        q = db.table(table).select("*")
        for key, value in filters.items():
            q = q.eq(key, value)
        if order:
            q = q.order(order, desc=True)
        return q.limit(limit).execute().data or []
    result = _db_read_with_retry(table, "query", query)
    return result if result is not None else []

def db_insert(table: str, payload: dict) -> dict | None:
    if db is None:
        return None
    try:
        # Do not retry inserts: a timeout may occur after the server committed, duplicating rows.
        result = db.table(table).insert(payload).execute()
        return (result.data or [None])[0]
    except Exception:
        log.exception("Supabase insert failed: %s", table)
        return None

def db_upsert(table: str, payload: dict, on_conflict: str) -> dict | None:
    if db is None:
        return None
    def query():
        result = db.table(table).upsert(payload, on_conflict=on_conflict).execute()
        return (result.data or [None])[0]
    return _db_read_with_retry(table, "upsert", query)

# Audit writes must exist for every moderation command. Keep non-critical audit calls
# off the event loop; commands that need confirmation can await audit_wait().
def _audit_payload(chat_id: int, actor_id: int, action: str, target_id: int | None = None,
                   details: str | None = None) -> dict[str, Any]:
    return {
        "chat_id": int(chat_id),
        "actor_id": int(actor_id or 0),
        "action": str(action)[:100],
        "target_id": int(target_id) if target_id is not None else None,
        "details": str(details)[:2000] if details is not None else None,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }

def audit(chat_id: int, actor_id: int, action: str, target_id: int | None = None,
          details: str | None = None) -> None:
    payload = _audit_payload(chat_id, actor_id, action, target_id, details)
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        db_insert("bot_audit", payload)
        return
    # Audit logging must not add a Supabase round-trip to unrelated commands.
    loop.create_task(asyncio.to_thread(db_insert, "bot_audit", payload))

async def audit_wait(chat_id: int, actor_id: int, action: str, target_id: int | None = None,
                     details: str | None = None) -> bool:
    """Persist an audit event without blocking Telegram update processing."""
    payload = _audit_payload(chat_id, actor_id, action, target_id, details)
    return bool(await asyncio.to_thread(db_insert, "bot_audit", payload))

def ensure_user(user_id: int, chat_id: int, username: str | None, first_name: str) -> dict:
    current = db_one("bot_users", user_id=user_id, chat_id=chat_id)
    if current:
        try:
            stored_rank = max(0, min(5, int(current.get("rank", 0) or 0)))
        except (TypeError, ValueError):
            stored_rank = 0
        rank = 4 if user_id == OWNER_ID else stored_rank
        db_upsert("bot_users", {
            "user_id": user_id, "chat_id": chat_id, "username": username,
            "first_name": first_name or "Пользователь", "rank": rank,
            "last_seen": datetime.now(timezone.utc).isoformat(),
        }, "user_id,chat_id")
        current.update({"username": username, "first_name": first_name or "Пользователь", "rank": rank})
        invalidate_user_rank(chat_id, user_id)
        return current
    rank = 4 if user_id == OWNER_ID else 0
    row = db_upsert("bot_users", {
        "user_id": user_id, "chat_id": chat_id, "username": username,
        "first_name": first_name or "Пользователь", "warns": 0, "rank": rank,
        "messages": 0, "last_seen": datetime.now(timezone.utc).isoformat(),
    }, "user_id,chat_id")
    invalidate_user_rank(chat_id, user_id)
    return row or {"user_id": user_id, "chat_id": chat_id, "warns": 0, "rank": rank, "messages": 0}

def set_warns(chat_id: int, user_id: int, amount: int) -> int:
    user = db_one("bot_users", user_id=user_id, chat_id=chat_id) or {
        "user_id": user_id, "chat_id": chat_id, "username": None,
        "first_name": "Пользователь", "rank": 0, "messages": 0,
    }
    new_count = max(0, int(user.get("warns", 0)) + amount)
    db_upsert("bot_users", {**user, "user_id": user_id, "chat_id": chat_id, "warns": new_count}, "user_id,chat_id")
    return new_count

_WARN_RECENT: dict[tuple[int, int, int], float] = {}

def active_warn_count(chat_id: int, user_id: int) -> int:
    """Count warnings from the last seven days; a clear/unwarn event adjusts the active ledger."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=7)
    rows = db_rows("bot_audit", limit=1000, order="created_at", chat_id=chat_id, target_id=user_id)
    relevant = []
    for row in rows:
        action = str(row.get("action") or "").lower()
        if action not in {"warn", "unwarn", "clearwarns"}:
            continue
        try:
            stamp = datetime.fromisoformat(str(row.get("created_at") or "").replace("Z", "+00:00"))
            if stamp.tzinfo is None:
                stamp = stamp.replace(tzinfo=timezone.utc)
        except Exception:
            continue
        if stamp >= cutoff:
            relevant.append((stamp, action))
    if not relevant:
        # Warnings older than seven days are expired, including legacy counter-only warnings.
        return 0
    count = 0
    for _, action in sorted(relevant):
        if action == "warn":
            count += 1
        elif action == "unwarn":
            count = max(0, count - 1)
        elif action == "clearwarns":
            count = 0
    return count

# ── Shared helpers ───────────────────────────────────────────────────────────
def is_group(message: Message) -> bool:
    return message.chat.type in (ChatType.GROUP, ChatType.SUPERGROUP)

async def is_owner(user_id: int) -> bool:
    return user_id == OWNER_ID

_CHAT_MEMBER_CACHE: dict[tuple[int, int], tuple[Any | None, float]] = {}
_CHAT_MEMBER_CACHE_TTL = 8.0

async def get_chat_member_cached(bot: Bot, chat_id: int, user_id: int) -> Any | None:
    """Avoid duplicate Telegram getChatMember round-trips for the same command."""
    key = (chat_id, user_id)
    now = time.monotonic()
    cached = _CHAT_MEMBER_CACHE.get(key)
    if cached and now - cached[1] < _CHAT_MEMBER_CACHE_TTL:
        return cached[0]
    try:
        member = await bot.get_chat_member(chat_id, user_id)
    except (TelegramBadRequest, TelegramForbiddenError):
        _CHAT_MEMBER_CACHE[key] = (None, now)
        return None
    _CHAT_MEMBER_CACHE[key] = (member, now)
    if len(_CHAT_MEMBER_CACHE) > 5000:
        cutoff = now - _CHAT_MEMBER_CACHE_TTL * 3
        for old_key, (_, stamp) in list(_CHAT_MEMBER_CACHE.items()):
            if stamp < cutoff:
                _CHAT_MEMBER_CACHE.pop(old_key, None)
    return member

async def is_admin(bot: Bot, chat_id: int, user_id: int) -> bool:
    if user_id == OWNER_ID:
        return True
    member = await get_chat_member_cached(bot, chat_id, user_id)
    return bool(member and member.status in (ChatMemberStatus.CREATOR, ChatMemberStatus.ADMINISTRATOR))

async def await_bot_rights_hint(chat_id: int) -> str:
    """Report actual bot permissions instead of always claiming it is not an admin."""
    try:
        me = await bot.get_me()
        member = await bot.get_chat_member(chat_id, me.id)
        if member.status not in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR):
            return "⚠️ <b>Pear CM пока не администратор.</b> Назначь его администратором для команд модерации."
        missing = []
        if not getattr(member, "can_delete_messages", False):
            missing.append("удаление сообщений")
        if not getattr(member, "can_restrict_members", False):
            missing.append("ограничение/блокировка участников")
        if missing:
            return "⚠️ <b>Pear CM уже администратор</b>, но не хватает прав: " + ", ".join(missing) + "."
        return "✅ <b>Pear CM уже администратор</b>; основные права модерации включены."
    except Exception:
        return "⚠️ Не удалось проверить права автоматически. Напиши /checkrights в группе."

_USER_RANK_CACHE: dict[tuple[int, int], tuple[int, float]] = {}
_USER_RANK_CACHE_TTL = 20.0

def user_rank(chat_id: int, user_id: int) -> int:
    if user_id == OWNER_ID:
        return 5
    key = (chat_id, user_id)
    now = time.monotonic()
    cached = _USER_RANK_CACHE.get(key)
    if cached and now - cached[1] < _USER_RANK_CACHE_TTL:
        return cached[0]
    row = db_one("bot_users", user_id=user_id, chat_id=chat_id) or {}
    try:
        rank = max(0, min(5, int(row.get("rank", 0))))
    except (TypeError, ValueError):
        rank = 0
    _USER_RANK_CACHE[key] = (rank, now)
    if len(_USER_RANK_CACHE) > 10000:
        cutoff = now - _USER_RANK_CACHE_TTL * 3
        for old_key, (_, stamp) in list(_USER_RANK_CACHE.items()):
            if stamp < cutoff:
                _USER_RANK_CACHE.pop(old_key, None)
    return rank

def invalidate_user_rank(chat_id: int, user_id: int) -> None:
    _USER_RANK_CACHE.pop((chat_id, user_id), None)

async def require_admin(message: Message, min_rank: int = 1) -> bool:
    if not message.from_user or not is_group(message):
        await message.answer("Эта команда работает в группе.")
        return False
    # .дк must actually restrict a command even for Telegram admins; the chat
    # creator and configured bot owner remain emergency overrides.
    key = moderation_command_key(message)
    try:
        # Fast path for hot chats; a cache miss stays off the event loop.
        now = time.monotonic()
        cached_settings = _CHAT_SETTINGS_CACHE.get(message.chat.id)
        cached_at = _CHAT_SETTINGS_CACHE_TIME.get(message.chat.id, 0.0)
        if cached_settings is not None and now - cached_at < _CHAT_SETTINGS_CACHE_TTL:
            settings = dict(cached_settings)
        else:
            settings = await asyncio.to_thread(get_chat_settings, message.chat.id)
        custom_rank = settings.get(f"modrank_{key}") if key else None
        if custom_rank is None and key:
            custom_rank = settings.get("modrank_default")
        personal = settings.get("personal_command_access", {})
        per_command = personal.get(key, {}) if isinstance(personal, dict) and key else {}
        personal_rule = per_command.get(str(message.from_user.id)) if isinstance(per_command, dict) else None
        if personal_rule == "deny":
            if message.from_user.id == OWNER_ID:
                return True
            denied_member = await get_chat_member_cached(bot, message.chat.id, message.from_user.id)
            if denied_member and denied_member.status == ChatMemberStatus.CREATOR:
                return True
            await message.answer("⛔ Для тебя отдельно запрещена эта команда администратором.")
            return False
        if personal_rule == "allow":
            if message.from_user.id == OWNER_ID:
                return True
            return True
        if custom_rank is not None:
            min_rank = max(1, min(5, int(custom_rank)))
    except (TypeError, ValueError):
        custom_rank = None
    if message.from_user.id == OWNER_ID:
        return True
    try:
        member = await get_chat_member_cached(bot, message.chat.id, message.from_user.id)
        is_creator = bool(member and member.status == ChatMemberStatus.CREATOR)
        is_telegram_admin = bool(member and member.status == ChatMemberStatus.ADMINISTRATOR)
    except Exception:
        is_creator = False
        is_telegram_admin = False
    rank = await asyncio.to_thread(user_rank, message.chat.id, message.from_user.id)
    if custom_rank is not None:
        if is_creator or rank >= min_rank:
            return True
        role = role_name(min_rank)
        await message.answer(f"⛔ Для этой команды нужен ранг не ниже {min_rank} — {role}.")
        return False
    if is_creator or is_telegram_admin:
        return True
    if rank >= min_rank:
        return True
    await message.answer(f"⛔ Для этой команды нужен ранг не ниже {min_rank} — {role_name(min_rank)} или права администратора Telegram.")
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
    "с": 1, "сек": 1, "секунда": 1, "секунды": 1, "секунд": 1, "секунду": 1,
    "секундами": 1, "секундах": 1, "секундочку": 1, "секундочка": 1,
    "s": 1, "sec": 1, "secs": 1, "second": 1, "seconds": 1,
    "м": 60, "мин": 60, "минута": 60, "минуты": 60, "минут": 60, "минуту": 60,
    "минутами": 60, "минутах": 60, "минуточку": 60, "минуточка": 60,
    "m": 60, "min": 60, "mins": 60, "minute": 60, "minutes": 60,
    "ч": 3600, "час": 3600, "часа": 3600, "часов": 3600, "часе": 3600, "часу": 3600,
    "часами": 3600, "часах": 3600, "часик": 3600, "часика": 3600, "часиков": 3600,
    "h": 3600, "hr": 3600, "hrs": 3600, "hour": 3600, "hours": 3600,
    "д": 86400, "дн": 86400, "день": 86400, "дня": 86400, "дней": 86400, "денька": 86400,
    "днём": 86400, "днем": 86400, "днями": 86400, "днях": 86400, "дню": 86400,
    "сутки": 86400, "суток": 86400, "сутками": 86400, "сутках": 86400, "сутку": 86400,
    "day": 86400, "days": 86400, "d": 86400,
    "н": 604800, "нед": 604800, "неделя": 604800, "недели": 604800, "неделю": 604800,
    "недель": 604800, "неделями": 604800, "неделях": 604800,
    "week": 604800, "weeks": 604800, "w": 604800,
    "мес": 2592000, "месяц": 2592000, "месяца": 2592000, "месяцев": 2592000, "месяцу": 2592000,
    "месяцами": 2592000, "месяцах": 2592000,
    "month": 2592000, "months": 2592000,
    "г": 31536000, "год": 31536000, "года": 31536000, "лет": 31536000, "году": 31536000,
    "годом": 31536000, "годами": 31536000, "годах": 31536000,
    "years": 31536000, "year": 31536000, "y": 31536000,
}

def _unit_seconds(unit: str) -> int | None:
    unit = unit.lower().strip().rstrip(".,")
    if unit in _TIME_UNITS:
        return _TIME_UNITS[unit]
    # Resolve Russian cases/plurals: секундами, минутами, часами, днями,
    # неделями, месяцами, годами, годами/годах, and common English forms.
    for key, seconds in sorted(_TIME_UNITS.items(), key=lambda item: len(item[0]), reverse=True):
        if len(key) >= 3 and unit.startswith(key):
            return seconds
    stems = (
        (("секунд", "секунд", "секунд"), 1),
        (("минут", "минут", "минут"), 60),
        (("час", "час", "час"), 3600),
        (("дн", "день", "дня", "дней", "днями", "днях", "сут"), 86400),
        (("недел", "недель", "неделями", "неделях"), 604800),
        (("месяц", "месяц", "месяцами", "месяцах", "месяцев"), 2592000),
        (("год", "года", "годами", "годах", "лет"), 31536000),
    )
    for prefixes, seconds in stems:
        if any(unit.startswith(prefix) for prefix in prefixes):
            return seconds
    return None

def parse_duration(value: str) -> timedelta | None:
    """Parse one or more duration units: '1 час 30 минут', '2д', '1 год'."""
    parsed = parse_duration_prefix((value or "").split())
    return parsed[0] if parsed else None

def parse_duration_prefix(tokens: list[str]) -> tuple[timedelta, int, str] | None:
    """Parse shorthand and natural Russian duration phrases; consumed counts original tokens."""
    number_words = {
        "один": 1, "одна": 1, "одно": 1, "одну": 1, "одного": 1, "одной": 1, "одном": 1,
        "два": 2, "две": 2, "двух": 2, "три": 3, "трех": 3, "трёх": 3,
        "четыре": 4, "четырех": 4, "четырёх": 4, "пять": 5, "пяти": 5,
        "шесть": 6, "шести": 6, "семь": 7, "семи": 7, "восемь": 8, "восьми": 8,
        "девять": 9, "девяти": 9, "десять": 10, "десяти": 10,
    }
    total = 0
    consumed = 0
    shown: list[str] = []
    i = 0
    while i < len(tokens):
        token = tokens[i].strip().lower().strip(",.;:")
        if total == 0 and token == "в" and i + 1 < len(tokens) and tokens[i + 1].lower().strip(",.;:") == "течение":
            consumed += 2
            i += 2
            continue
        if total == 0 and token == "сроком" and i + 1 < len(tokens) and tokens[i + 1].lower().strip(",.;:") == "на":
            consumed += 2
            i += 2
            continue
        if total == 0 and token == "на":
            consumed += 1
            i += 1
            continue
        if token == "и" and total > 0:
            consumed += 1
            i += 1
            continue
        word_amount = number_words.get(token)
        numeric = re.fullmatch(r"(\d{1,7})\s*([a-zа-яё]+)?", token, re.I)
        if numeric or word_amount is not None:
            amount = int(numeric.group(1)) if numeric else int(word_amount)
            unit = numeric.group(2) if numeric else None
            used = 1
            if not unit:
                if i + 1 >= len(tokens):
                    break
                possible_unit = tokens[i + 1].lower().strip(",.;:")
                seconds = _unit_seconds(possible_unit)
                if seconds is None:
                    break
                unit = possible_unit
                used = 2
            else:
                seconds = _unit_seconds(unit)
                if seconds is None:
                    break
        else:
            seconds = _unit_seconds(token)
            if seconds is None:
                break
            amount = 1
            unit = token
            used = 1
        if amount < 1:
            return None
        total += amount * seconds
        shown.append(f"{amount} {unit}")
        consumed += used
        i += used
        if i >= len(tokens):
            break
        next_token = tokens[i].strip().lower().strip(",.;:")
        if next_token != "и" and not re.match(r"^\d", next_token) and next_token not in number_words and _unit_seconds(next_token) is None:
            break
    # Telegram treats bans at/over the 366-day boundary as permanent; cap timed bans at 365 days.
    if consumed == 0 or total <= 0 or total > 365 * 86400:
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

_RECENT_CHAT_USERS: dict[tuple[int, int], tuple[str | None, str, float]] = {}
_RECENT_CHAT_USERS_TTL = 6 * 60 * 60

async def resolve_target(message: Message, args: list[str]) -> tuple[int, str] | None:
    def display_name(user_id: int, username: str | None, first_name: str | None, fallback: str) -> str:
        if username:
            return f"@{username.lstrip('@')}"
        return first_name or fallback or f"ID {user_id}"

    if message.reply_to_message and message.reply_to_message.from_user:
        u = message.reply_to_message.from_user
        if not u.is_bot:
            return u.id, display_name(u.id, u.username, u.full_name, str(u.id))

    # Accept a reply, ID, @username, plain username, or exact displayed name.
    # Numeric rank/duration arguments are ignored rather than mistaken for user IDs.
    for raw_value in args:
        raw = raw_value.strip().strip(",;:")
        raw = re.sub(r"^(?:id\s*[:=]?\s*)", "", raw, flags=re.I)
        if raw.isdigit() and len(raw) >= 7:
            uid = int(raw)
            row = await asyncio.to_thread(db_one, "bot_users", user_id=uid, chat_id=message.chat.id)
            if row:
                return uid, display_name(uid, row.get("username"), row.get("first_name"), raw)
            return uid, f"ID {uid}"

    candidates = []
    for raw_value in args:
        raw = raw_value.strip().strip(",;:")
        if not raw or raw.isdigit():
            continue
        raw = re.sub(r"^(?:https?://)?t\.me/", "", raw, flags=re.I)
        candidates.append(raw.lstrip("@").casefold())
    # Exact full-name match supports display names containing spaces, without
    # treating arbitrary reason text as a target unless it matches a real user.
    joined = " ".join(str(value).strip().strip(",;:") for value in args).strip()
    if joined and not joined.isdigit():
        candidates.append(joined.lstrip("@").casefold())
    if not candidates:
        return None

    now = time.monotonic()
    for (chat_id, uid), (username, first_name, stamp) in list(_RECENT_CHAT_USERS.items()):
        if now - stamp > _RECENT_CHAT_USERS_TTL:
            _RECENT_CHAT_USERS.pop((chat_id, uid), None)
            continue
        if chat_id != message.chat.id:
            continue
        user_name = (username or "").lstrip("@").casefold()
        first = (first_name or "").casefold()
        if any(candidate == user_name or candidate == first for candidate in candidates):
            return uid, display_name(uid, username, first_name, candidates[0])

    rows = await asyncio.to_thread(db_rows, "bot_users", limit=1000, chat_id=message.chat.id)
    for row in rows:
        try:
            uid = int(row.get("user_id", 0) or 0)
        except (TypeError, ValueError):
            continue
        if uid <= 0:
            continue
        username = str(row.get("username") or "")
        first_name = str(row.get("first_name") or "")
        user_name = username.lstrip("@").casefold()
        first = first_name.casefold()
        if any(candidate == user_name or candidate == first for candidate in candidates):
            return uid, display_name(uid, username, first_name, candidates[0])
    return None

def args_after_command(message: Message) -> list[str]:
    text_value = (message.text or "").strip()
    if not text_value:
        return []
    # Remove /command@botname before parsing arguments.
    text_value = re.sub(r"^/[A-Za-z_]\w*(?:@\w+)?\s*", "", text_value, count=1)
    # Multi-word commands first, including declensions of «предупреждение» and «мут».
    aliases = (
        r"^(?:снять|сними|снимите|снимайте|убрать|убери|уберите|удалить|удали|удалите)\s+(?:варн(?:а|у|ом|е|ы|ов|ами|ах)?|предупреждени(?:е|я|ю|ем|и)|мут(?:а|у|ом|е|ы|ов|ами|ах)?|бан(?:а|у|ом|е)?)\s*",
        r"^(?:дать|дай|дайте|выдать|выдай|выдайте|поставить|поставь|поставьте)\s+(?:мут|варн|бан|кик|предупреждени(?:е|я|ю|ем|и))\s*",
    )
    for pattern in aliases:
        stripped = re.sub(pattern, "", text_value, flags=re.I)
        if stripped != text_value:
            return stripped.split()
    text_value = re.sub(
        r"^[.!/]?(?:мут(?:ить|а|у|ом|е|ы|ов|ами|ах)?|замут(?:ить|и|ь|ьте|ите|а|у|ом|е)?|замуч(?:ить|и|ь|ьте|ите)?|заглуш(?:ить|и|ь|ьте|ите)?|размут(?:ить|и|ь|ьте|ите|а|у)?|анмут(?:ить|и|ь|ьте|ите)?|унмут|бан(?:ить|а|у|ом|е|ы|ов|ами|ах)?|забан(?:ить|ь|ьте|ьте|ьте)?|блок(?:ировать|ируй|ируйте|ни|нуть)?|заблок(?:ировать|ируй|ируйте|ируйте)?|разбан(?:ить|а|у|ь|ьте)?|кик(?:нуть|ни|нись|а|у|ом|е|и|ов|ами)?|исключ(?:ить|и|ите)|выгнать|выгони|выгоните|выдвор(?:ить|и|ите)|изгнать|изгони|варн(?:ить|а|у|ом|е|ы|ов|ами)?|предупред(?:ить|и|ите|и́)|предупреждени(?:е|я|ю|ем|и)|предупреждай|предупреждайте|анварн|снятьварн|убратьварн|повысить(?:\s*[- ]?\s*ранг)?|повышени(?:е|я|ю|ем|и)|повышай|поднять(?:\s+ранг)?|продвинуть|назначить\s+(?:ранг|чин)|присвоить\s+ранг|понизить\s+в\s+должности|понизить(?:\s*[- ]?\s*ранг)?|понижени(?:е|я|ю|ем|и)|понижай|понизь|снять\s+(?:ранг|чин|админ(?:а|у|ом|е)?|администратор(?:а|у|ом|е)?|модератор(?:а|у|ом|е)?)|убрать\s+(?:ранг|чин)|разжаловать|лишить\s+ранга|очистить|чистка|пин(?:ить|а|у|ом|е)?|закрепить|анпин|открепить)\s*",
        "", text_value, count=1, flags=re.I
    )
    return text_value.split()


def role_name(rank: int) -> str:
    return {0: "Участник", 1: "Младший модератор", 2: "Старший модератор",
            3: "Младший администратор", 4: "Старший администратор",
            5: "Создатель"}.get(max(0, min(5, int(rank))), "Участник")

def moderation_command_key(message: Message) -> str | None:
    text_value = re.sub(r"^[.!/]+", "", (message.text or "").strip().lower())
    text_value = re.sub(r"@\w+", "", text_value)
    words = text_value.split()
    if not words:
        return None
    first = words[0]
    # Also recognize «@user мут 1 час» and multi-word forms like «снять варн».
    if first.startswith("@") and len(words) > 1:
        first = words[1]
        words = words[1:]
    if first in {"снять", "убрать", "удалить"} and len(words) > 1:
        second = words[1]
        if second in {"варн", "предупреждение"}:
            return "unwarn"
        if second in {"мут", "муту"}:
            return "unmute"
        first = second
    elif first in {"дать", "выдать"} and len(words) > 1:
        first = words[1]
    return {"мут":"mute", "замутить":"mute", "замуть":"mute", "замут":"mute",
            "бан":"ban", "забанить":"ban", "забан":"ban", "кик":"kick", "кикнуть":"kick", "выгнать":"kick",
            "варн":"warn", "предупредить":"warn", "предупреждение":"warn",
            "анварн":"unwarn", "снятьварн":"unwarn", "убратьварн":"unwarn",
            "размут":"unmute", "анмут":"unmute", "унмут":"unmute", "снятьмут":"unmute", "убратьмут":"unmute",
            "разбан":"unban", "повысить":"promote", "повыситьранг":"promote", "понизить":"demote",
            "понизитьранг":"demote", "очистить":"purge", "-смс":"delete", "пин":"pin", "анпин":"unpin"}.get(first,
            first if first in {"mute","unmute","ban","unban","kick","warn","unwarn","clearwarns","promote","demote","purge","delete","pin","unpin"} else None)

def required_rank_for(message: Message, default_rank: int) -> int:
    key = moderation_command_key(message)
    if not key:
        return default_rank
    try:
        settings = get_chat_settings(message.chat.id)
        configured = settings.get(f"modrank_{key}")
        if configured is None:
            configured = settings.get("modrank_default", default_rank)
        return max(1, min(5, int(configured)))
    except (TypeError, ValueError):
        return default_rank

async def bot_can_restrict(message: Message) -> bool:
    try:
        me = await bot.get_me()
        member = await bot.get_chat_member(message.chat.id, me.id)
        return member.status in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR)
    except Exception:
        return False

_ERROR_NOTICE_LAST: dict[tuple[int, str], float] = {}
_COMMAND_LATENCIES: deque[tuple[float, str]] = deque(maxlen=500)
_ERROR_NOTICE_TTL = 30.0

async def safe_error(message: Message, exc: Exception) -> None:
    detail = str(exc or "").strip()
    lowered = detail.lower()
    if isinstance(exc, TelegramForbiddenError):
        user_text = "⛔ Telegram запретил действие: у бота нет доступа к чату или необходимых прав. Проверьте, что Pear CM — администратор и у него включено «Ограничение участников»."
    elif isinstance(exc, TelegramBadRequest) and ("administrator" in lowered or "chat owner" in lowered):
        user_text = "⛔ Цель — администратор или создатель чата. Telegram не разрешает ограничивать её этим действием."
    elif isinstance(exc, TelegramBadRequest) and any(term in lowered for term in ("not enough rights", "have no rights", "can't restrict", "cannot restrict", "not an admin")):
        user_text = "⛔ У Pear CM не хватает прав. Назначьте его администратором и включите право «Ограничение участников»."
    elif isinstance(exc, TelegramBadRequest) and any(term in lowered for term in ("user not found", "participant_id_invalid", "chat not found")):
        user_text = "⛔ Не удалось найти участника в этом чате. Ответьте на его сообщение или укажите ID участника, которого бот знает."
    elif isinstance(exc, TelegramBadRequest):
        user_text = f"⚠️ Telegram отклонил действие: <code>{escape(detail[:220] or 'неизвестная причина')}</code>"
    else:
        user_text = f"⚠️ Техническая ошибка <code>{type(exc).__name__}</code>. Подробности записаны в журнал."
    log.warning("Moderation action failed (%s): %s", type(exc).__name__, detail[:500])
    now = time.monotonic()
    notice_key = (message.chat.id, f"{type(exc).__name__}:{detail[:180]}")
    last_notice = _ERROR_NOTICE_LAST.get(notice_key, 0.0)
    if now - last_notice < _ERROR_NOTICE_TTL:
        return
    _ERROR_NOTICE_LAST[notice_key] = now
    if len(_ERROR_NOTICE_LAST) > 2000:
        for key, stamp in list(_ERROR_NOTICE_LAST.items()):
            if now - stamp > _ERROR_NOTICE_TTL * 4:
                _ERROR_NOTICE_LAST.pop(key, None)
    try:
        await message.answer(user_text)
    except Exception:
        log.exception("Could not send moderation error response")

# ── Welcome and menu ─────────────────────────────────────────────────────────
@router.message(CommandStart())
@router.message(F.text == "🚀 Начать пользоваться ботом")
async def start(message: Message) -> None:
    user_id = message.from_user.id if message.from_user else None
    if message.chat.type == ChatType.PRIVATE:
        text = (
            "👋 <b>Привет! Я Pear CM</b> — бот для управления группой.\n\n"
            "Добавьте меня в группу и выдайте права администратора, если нужны "
            "муты, баны, удаление сообщений и закрепление.\n\n"
            "Откройте <b>🔗 Команды</b>, чтобы посмотреть возможности.\n\n"
            "💚 Если хотите поддержать разработку, можете попробовать мой VPN: "
            '<a href="https://t.me/awesomeproxyvpn_bot">@awesomeproxyvpn_bot</a>. Буду очень рад поддержке!'
        )
    else:
        # Keep /start fast: do not wait for an extra Telegram getChatMember
        # request just to render the welcome message.
        text = (
            f"👋 Pear CM подключён к чату <b>{message.chat.title or 'группа'}</b>!\n\n"
            "Нажми кнопку ниже — отвечу на запрос.\n\n"
            "Открой кнопки под этим сообщением, чтобы пользоваться ботом."
        )

    # Send exactly one visible message, with the inline keyboard attached.
    # Do not send a temporary ReplyKeyboardRemove message: Telegram displays
    # it briefly even if we delete it immediately.
    await message.answer(text, reply_markup=welcome_inline_keyboard(user_id, message.chat.type))

@router.message(F.chat.type == ChatType.PRIVATE, F.text == "🛠 Админ-панель")
async def admin_panel_button(message: Message) -> None:
    if not message.from_user or message.from_user.id != OWNER_ID:
        await message.answer("⛔ Админ-панель доступна только владельцу бота в личных сообщениях.")
        return
    await owner_panel(message)

@router.message(Command("help", "commands"))
@router.message(F.text.regexp(r"(?i)^/(?:help|commands)(?:@\w+)?(?:\s|$)"))
@router.message(F.text.regexp(r"(?i)^[.!/]?(?:помощь|команды)(?:\s|$)"))
@router.message(F.text.in_({"🔗 Команды", "❓ Помощь"}))
async def help_command(message: Message) -> None:
    user_id = message.from_user.id if message.from_user else None
    is_private = message.chat.type == ChatType.PRIVATE
    is_privileged = bool(
        is_private and user_id and (
            user_id == OWNER_ID or await asyncio.to_thread(_operator_active, user_id)
        )
    )
    help_text = (
        "<b>📚 Команды Pear CM</b>\n\n"
        "<b>Профиль и чат</b>\n"
        "• /profile или профиль — профиль; топ дня/недели/вся — рейтинг текущего чата; топ все чаты — общий рейтинг\n"
        "• /admins или админы / кто админ — список админов\n"
        "• /warns или варны — мои предупреждения; /marry или брак — брак в ответ на сообщение\n\n"
        "<b>Модерация (команды можно писать по-русски или по-английски)</b>\n"
        "• ответьте на сообщение: «мут 1 час спам» или «/mute 1h spam» — мут на 1 час с причиной\n"
        "• /unmute или размут — снять мут; /ban или бан — бан; /unban или разбан — разбан\n"
        "• Разбан: ответом на сообщение, либо «разбан 123456789» / «/unban 123456789»\n"
        "• /kick или кик — исключить; /warn или варн — выдать предупреждение\n"
        "• /unwarn, анварн, снять варн, убрать варн — снять одно предупреждение; /clearwarns или сбросварнов — обнулить\n"
        "• Повысить / понизить — изменить внутренний ранг на один уровень; /rank @user 3 — назначить ранг\n"
        "• .дк мут 4 — установить минимальный ранг для команды (1–5)\n"
        "• Ранги: 1 — младший модератор, 2 — старший модератор, 3 — младший администратор, 4 — старший администратор, 5 — создатель\n"
        "• /del или «-смс» — удалить сообщение; /purge 10 или «очистить 10» — очистить сообщения\n"
        "• /pin или «.пин» — закрепить сообщение; /unpin или «.анпин» — открепить\n"
        "• /info или инфо — информация об участнике; /logs или логи — последние действия\n"
        "• +антиссылка / -антиссылка — управление фильтрами (и другие +анти…/-анти…)\n"
        "• /settrigger 3 mute 1ч — настройка наказания за варны; /rank — ваш ранг\n"
        "• /time или дата — текущее московское время (Europe/Moscow, UTC+3); /calc 2+2 — калькулятор; /random 1 100 — случайное число; /dice — кубик\n"
        "• /activity — активность; /quote ответом — цитата; /favorites ответом — избранное; /achievements — достижения; /suggest текст — предложение\n"
        "• врембан день / врембан 1 день / врембан месяц / врембан год / врембан 2ч причина — временный бан до 365 дней; заметка текст ответом — заметка о пользователе; заметки ответом — список заметок\n"
        "• доверенный ответом — добавить в исключения антиспама; доверенные — список; недоверенный ответом — убрать\n"
        "• причина добавить мут спам Текст — шаблоны причин; права мут 3 — минимальный ранг команды\n"
        "• приветствие Текст {name} — приветствие новичков; расписание 23:00 08:00 — автоматическое закрытие/открытие\n"
        "• экспорт настроек / импорт настроек — перенос настроек; статистика модераторов неделя — отчёт модерации\n"
        "• история наказаний ответом — история пользователя; диагностика — состояние и задержки обработчиков (для овнера/операторов)\n\n"
        "<b>Важно:</b> команды модерации требуют прав администратора. "
        "Для mute/ban/delete/pin сам бот тоже должен иметь соответствующие права."
    )
    # Owner/operator commands are only disclosed in the private bot chat.
    if is_privileged:
        help_text += (
            "\n\n<b>Управление ботом</b>: /owner, /status, /ping, /diag, "
            "/logs, /globalstats, /modules, /operator ID, /maintenance on|off"
        )
    await message.answer(
        help_text,
        reply_markup=main_keyboard(user_id, message.chat.type, is_privileged),
    )

@router.message(F.text.in_({"📋 Меню", "Меню"}))
async def menu_button(message: Message) -> None:
    await message.answer(
        "👋 <b>Главное меню Pear CM</b>\n\n"
        "➕ Добавить в чат — выберите группу и подтвердите права бота.\n"
        "💠 Профиль — ваша статистика и предупреждения.\n"
        "🔗 Команды — список доступных команд.\n"
        "🛠 Установка — понятная инструкция по подключению.",
        reply_markup=main_keyboard(
            message.from_user.id if message.from_user else None,
            message.chat.type,
            bool(
                message.chat.type == ChatType.PRIVATE and message.from_user and (
                    message.from_user.id == OWNER_ID
                    or await asyncio.to_thread(_operator_active, message.from_user.id)
                )
            ),
        ),
    )

@router.message(F.text == "➕ Добавить в чат")
async def add_to_chat(message: Message) -> None:
    if message.chat.type != ChatType.PRIVATE:
        await message.answer("Откройте бота в личных сообщениях и нажмите «➕ Добавить в чат». ⚠️ Чтобы команды модерации работали в этой группе, выдайте Pear CM права администратора с необходимыми разрешениями.")
        return
    try:
        me = await bot.get_me()
        add_url = f"https://t.me/{me.username}?startgroup=true"
        keyboard = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="➕ Выбрать группу и добавить бота", url=add_url)
        ]])
        await message.answer(
            "➕ <b>Добавление Pear CM в группу</b>\n\n"
            "1. Нажмите кнопку ниже и выберите группу.\n"
            "2. Подтвердите добавление бота.\n"
            "3. Для удаления сообщений, мутов и банов выдайте права администратора: «Удаление сообщений», «Блокировка пользователей» и «Ограничение участников».\n"
            "4. Чтобы закрывать и открывать чат, боту также нужны права управления чатом.\n\n"
            "Выдавайте только те права, которые действительно нужны.",
            reply_markup=keyboard,
        )
    except Exception as exc:
        await safe_error(message, exc)

@router.message(F.text == "🛠 Установка")
async def installation_help(message: Message) -> None:
    await message.answer(
        "🛠 <b>Установка Pear CM — пошагово</b>\n\n"
        "<b>1.</b> Нажмите «➕ Добавить в чат» в главном меню.\n"
        "<b>2.</b> Выберите нужную группу в Telegram и подтвердите добавление.\n"
        "<b>3.</b> Откройте настройки группы → Администраторы → Добавить администратора → Pear CM.\n"
        "<b>4.</b> Включите необходимые права: удаление сообщений, блокировка/ограничение участников. Для команд «-чат» и «+чат» нужны права управления чатом.\n"
        "<b>5.</b> Проверьте в группе: <code>/admins</code>, <code>-смс 10</code>, <code>-чат</code> и <code>+чат</code>.\n\n"
        "Если нужна только статистика, не выдавайте права модерации. Команды доступны в группе через меню «/».",
        reply_markup=main_keyboard(
            message.from_user.id if message.from_user else None,
            message.chat.type,
            bool(
                message.chat.type == ChatType.PRIVATE and message.from_user and (
                    message.from_user.id == OWNER_ID
                    or await asyncio.to_thread(_operator_active, message.from_user.id)
                )
            ),
        ),
    )

@router.callback_query(F.data.startswith("group:"))
async def group_menu_callback(callback: CallbackQuery) -> None:
    if not callback.message:
        await callback.answer()
        return
    action = (callback.data or "").split(":", 1)[-1]
    if action == "profile" and callback.from_user:
        user = await asyncio.to_thread(ensure_user, callback.from_user.id, callback.message.chat.id, callback.from_user.username, callback.from_user.full_name)
        body = (f"👤 <b>Профиль</b>\n{mention(callback.from_user.id, callback.from_user.full_name)}\n\n"
                f"💬 Сообщений: <b>{int(user.get('messages', 0))}</b>\n🏅 Ранг: <b>{int(user.get('rank', 0))}</b>\n⚠️ Предупреждений: <b>{int(user.get('warns', 0))}</b>\n\n"
                + await await_bot_rights_hint(callback.message.chat.id))
    else:
        body = ("📚 <b>Основные команды Pear CM</b>\n• /profile — профиль и статистика\n• /top — топ участников\n• /admins — список администраторов\n• /warns — предупреждения\n• /help — все команды\n\n"
                + await await_bot_rights_hint(callback.message.chat.id))
    await callback.answer()
    await callback.message.answer(body)

@router.message(F.text.in_({"🏅 Чаты: топ дня", "🏆 Супер топ чатов", "Топ чат", "Чаты топ дня"}))
async def removed_chat_top_button(message: Message) -> None:
    await message.answer("Эти рейтинги чатов убраны из меню. Используйте /top, чтобы посмотреть активных участников текущей группы.")

@router.message(F.chat.type != ChatType.PRIVATE, F.text == "🌐 Открыть приложение")
async def group_menu_text_fallback(message: Message) -> None:
    await message.answer("🌐 Открой Mini App через кнопку приложения в личке с Pear CM. В группе используй /help для команд.")

@router.message(F.text == "💠 Профиль")
@router.message(Command("profile"))
@router.message(F.text.regexp(r"(?i)^[.!/]?профиль(?:\s|$)"))
async def profile(message: Message) -> None:
    if not message.from_user:
        return
    chat_id = message.chat.id
    user = await asyncio.to_thread(ensure_user, message.from_user.id, chat_id, message.from_user.username, message.from_user.full_name)
    messages = int(user.get("messages", 0))
    rank = int(user.get("rank", 0))
    warns = await asyncio.to_thread(active_warn_count, message.chat.id, message.from_user.id)
    level_name = "Новичок" if messages < 25 else "Участник" if messages < 100 else "Активный" if messages < 500 else "Легенда чата"
    next_goal = next((goal for goal in (25, 100, 500, 1000) if messages < goal), None)
    progress = f"📈 До следующего рубежа: <b>{next_goal - messages}</b> сообщ." if next_goal else "✨ Достигнут высокий рубеж активности!"
    await message.answer(
        f"👤 <b>Профиль участника</b>\n{mention(message.from_user.id, message.from_user.full_name)}\n\n"
        f"💬 Сообщений: <b>{messages}</b>\n🏅 Ранг: <b>{rank}</b> · <b>{level_name}</b>\n⚠️ Предупреждения: <b>{warns}</b>\n{progress}\n\n"
        "💡 Подсказка: /top — рейтинг участников, /activity — статистика активности."

    )

@router.message(Command("time"))
@router.message(F.text.regexp(r"(?i)^[.!/]?(?:время|дата)(?:\s|$)"))
async def moscow_time_cmd(message: Message) -> None:
    now = moscow_now()
    await message.answer(
        "🕒 <b>Московское время</b>\n"
        f"{now.strftime('%d.%m.%Y')} · <b>{now.strftime('%H:%M:%S')} МСК</b>\n"
        "Часовой пояс: <code>Europe/Moscow</code> (UTC+3)\n"
        "Время обновляется при каждом вызове команды."
    )


@router.message(F.text.in_({"⚠️ Мои варны", "Варны"}))
@router.message(Command("warns"))
@router.message(F.text.regexp(r"(?i)^[.!/]?(?:варны|мои варны)(?:\s|$)"))
async def my_warns(message: Message) -> None:
    if not message.from_user:
        return
    user = await asyncio.to_thread(ensure_user, message.from_user.id, message.chat.id, message.from_user.username, message.from_user.full_name)
    if message.from_user.username:
        username = message.from_user.username.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        display = f'<a href="https://t.me/{username}">@{username}</a>'
    else:
        display = mention(message.from_user.id, message.from_user.full_name)
    active = await asyncio.to_thread(active_warn_count, message.chat.id, message.from_user.id)
    await message.answer(f"⚠️ {display}, у вас <b>{active}</b> действующих предупреждений. Срок каждого — 7 дней.")

@router.message(F.text == "💍 Брак")
async def marriage_menu(message: Message) -> None:
    await message.answer("💍 Чтобы предложить брак, ответьте на сообщение человека командой <code>/marry</code>.\nПосмотреть статус: <code>/marriage</code>.")

@router.message(F.text == "🛡 Кто админ")
@router.message(Command("admins"))
@router.message(F.text.regexp(r"(?i)^[.!/]?(?:админы|кто админ)(?:\s|$)"))
async def admins(message: Message) -> None:
    try:
        # Show Pear CM roles plus the real Telegram chat creator.
        ranked_users_result, telegram_admins_result = await asyncio.gather(
            asyncio.to_thread(db_rows, "bot_users", limit=1000, chat_id=message.chat.id),
            bot.get_chat_administrators(message.chat.id),
            return_exceptions=True,
        )
        ranked_users = ranked_users_result if isinstance(ranked_users_result, list) else []
        chat_creators: dict[int, str] = {}
        if isinstance(telegram_admins_result, BaseException):
            log.info("Could not fetch Telegram chat creator for chat %s: %s", message.chat.id, telegram_admins_result)
        else:
            for member in telegram_admins_result:
                if member.status == ChatMemberStatus.CREATOR:
                    chat_creators[member.user.id] = member.user.full_name or f"ID {member.user.id}"
        ranks: dict[int, tuple[int, str, str | None]] = {}
        for row in ranked_users:
            try:
                uid = int(row.get("user_id", 0))
                rank = max(0, min(5, int(row.get("rank", 0) or 0)))
            except (TypeError, ValueError):
                continue
            if uid > 0 and rank > 0:
                username = str(row.get("username") or "").lstrip("@") or None
                name = str(row.get("first_name") or (f"@{username}" if username else f"ID {uid}"))
                ranks[uid] = (rank, name, username)

        owner_row = next((r for r in ranked_users if int(r.get("user_id", 0) or 0) == OWNER_ID), {})
        owner_username = str(owner_row.get("username") or "").lstrip("@") or None
        if message.from_user and message.from_user.id == OWNER_ID:
            owner_username = owner_username or message.from_user.username
        owner_name = str(owner_row.get("first_name") or (f"@{owner_username}" if owner_username else ranks.get(OWNER_ID, (4, "Владелец Pear CM", None))[1]))
        ranks[OWNER_ID] = (4, owner_name, owner_username or ranks.get(OWNER_ID, (4, "", None))[2])

        # Clear five-step Pear CM hierarchy: rank 1 is the entry moderator, rank 5 is creator.
        rank_titles = {
            5: "👑 Создатель",
            4: "⭐⭐⭐⭐ Старший администратор",
            3: "⭐⭐⭐ Младший администратор",
            2: "⭐⭐ Старший модератор",
            1: "⭐ Младший модератор",
        }
        grouped: dict[int, list[tuple[int, str, str | None]]] = defaultdict(list)
        creator_ids = set(chat_creators)
        # The chat creator is listed separately and never duplicated in Pear CM ranks.
        for uid, entry in ranks.items():
            if uid not in creator_ids:
                grouped[entry[0]].append((uid, entry[1], entry[2]))
        lines = ["🛡 <b>Администраторы и модераторы Pear CM</b>", ""]
        if chat_creators:
            lines.append("<b>👑 Создатели чата</b>")
            for uid, name in sorted(chat_creators.items(), key=lambda item: item[1].casefold()):
                lines.append(f"👑 {profile_link(uid, str(name))} · создатель чата")
            lines.append("")
        for rank in (5, 4, 3, 2, 1):
            members = sorted(grouped.get(rank, []), key=lambda item: item[1].casefold())
            if not members:
                continue
            lines.append(f"<b>{rank_titles[rank]} · Pear CM</b>")
            icon = {5: "👑", 4: "🛡", 3: "🔰", 2: "🔹", 1: "🔸"}[rank]
            for uid, name, username in members:
                display_name = str(name or (f"@{username}" if username else f"ID {uid}"))
                lines.append(f"{icon} {profile_link(uid, display_name)}")
            lines.append("")
        if len(lines) <= 2:
            lines = ["🛡 <b>Администраторы и модераторы Pear CM</b>", "Пока не найдены создатели чата или назначенные роли Pear CM."]
        await message.answer("\n".join(lines).strip())
    except Exception:
        log.exception("Could not build Pear CM internal admin list")
        await message.answer("Не удалось получить список администраторов Pear CM. Попробуйте ещё раз.")

# ── Activity tracking and welcome ────────────────────────────────────────────
@router.message(F.new_chat_members)
async def welcome_members(message: Message) -> None:
    settings = await asyncio.to_thread(get_chat_settings, message.chat.id) if "get_chat_settings" in globals() else dict(FILTER_DEFAULTS)
    for user in message.new_chat_members:
        if user.is_bot:
            if settings.get("antibot"):
                audit(message.chat.id, 0, "antibot_detected", user.id,
                      "monitor_only; bot joined; no automatic ban or deletion")
                log.info("Bot joined chat=%s user=%s; antibot is monitor-only", message.chat.id, user.id)
            continue
        await asyncio.to_thread(ensure_user, user.id, message.chat.id, user.username, user.full_name)
        if settings.get("welcome_enabled", True):
            template = str(settings.get("welcome_message") or "👋 Добро пожаловать, {name}!")[:1500]
            safe_template = escape(template)
            safe_template = safe_template.replace("{name}", escape(user.full_name or "участник"))
            safe_template = safe_template.replace("{username}", escape("@" + user.username if user.username else user.full_name or "участник"))
            safe_template = safe_template.replace("{id}", str(user.id))
            try:
                await message.answer(safe_template)
            except Exception:
                await message.answer(f"👋 Добро пожаловать, {mention(user.id, user.full_name)}!")
    # Process anti-raid after welcoming; this is called directly because aiogram stops at the first matching service-message handler.
    await apply_raid_protection(message)

# Activity events are batched by the background writer below so ordinary messages
# never wait for Supabase writes.

def _persist_activity_batch_sync(items: list[tuple[int, int, str | None, str, str, int]]) -> None:
    """Atomically persist events and counters; the RPC is idempotent by chat/message ID."""
    if not items:
        return
    if db is None:
        raise RuntimeError("Supabase is not configured; activity batch remains queued")
    events = [{
        "chat_id": int(chat_id), "user_id": int(user_id), "username": username,
        "first_name": first_name or "Пользователь", "created_at": created_at,
        "message_id": int(message_id),
    } for chat_id, user_id, username, first_name, created_at, message_id in items]
    # A single Postgres transaction inserts unseen messages and updates daily/user counters.
    # If the network times out after commit, retrying the RPC won't count the messages twice.
    db.rpc("record_bot_activity_batch", {"p_items": events}).execute()

_ACTIVITY_QUEUE: asyncio.Queue[tuple[int, int, str | None, str, str, int]] = asyncio.Queue(maxsize=5000)
# Temporary bounded overflow absorbs bursts while the database writer catches up.
_ACTIVITY_OVERFLOW_MAX = 20000
_ACTIVITY_OVERFLOW: deque[tuple[int, int, str | None, str, str, int]] = deque()
_ACTIVITY_WRITE_SEMAPHORE = asyncio.Semaphore(4)
_ACTIVITY_WRITER_BUSY = False

async def _drain_activity_queue(timeout: float = 15.0) -> bool:
    """Give queued and overflow activity events a chance to persist before shutdown."""
    deadline = time.monotonic() + max(0.1, timeout)
    while time.monotonic() < deadline:
        if _ACTIVITY_QUEUE.empty() and not _ACTIVITY_OVERFLOW and not _ACTIVITY_WRITER_BUSY:
            try:
                await asyncio.wait_for(_ACTIVITY_QUEUE.join(), timeout=max(0.1, deadline - time.monotonic()))
                return True
            except asyncio.TimeoutError:
                return False
        await asyncio.sleep(0.05)
    log.warning("Activity queue did not fully drain before shutdown: queue=%s overflow=%s",
                _ACTIVITY_QUEUE.qsize(), len(_ACTIVITY_OVERFLOW))
    return False

async def _activity_writer_loop() -> None:
    global _ACTIVITY_WRITER_BUSY
    while True:
        if _ACTIVITY_OVERFLOW:
            first = _ACTIVITY_OVERFLOW.popleft()
            queued_count = 0
        else:
            first = await _ACTIVITY_QUEUE.get()
            queued_count = 1
        _ACTIVITY_WRITER_BUSY = True
        batch = [first]
        deadline = time.monotonic() + 0.75
        while len(batch) < 100:
            while _ACTIVITY_OVERFLOW and len(batch) < 100:
                batch.append(_ACTIVITY_OVERFLOW.popleft())
            if len(batch) >= 100:
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                batch.append(await asyncio.wait_for(_ACTIVITY_QUEUE.get(), timeout=min(remaining, 0.15)))
                queued_count += 1
            except asyncio.TimeoutError:
                if not _ACTIVITY_OVERFLOW:
                    break
        persisted = False
        try:
            async with _ACTIVITY_WRITE_SEMAPHORE:
                for attempt in range(3):
                    try:
                        await asyncio.to_thread(_persist_activity_batch_sync, batch)
                        persisted = True
                        break
                    except Exception:
                        log.exception("Activity batch persistence failed (attempt %s/3; size=%s)", attempt + 1, len(batch))
                        if attempt < 2:
                            await asyncio.sleep(0.5 * (attempt + 1))
        finally:
            if not persisted:
                room = max(0, _ACTIVITY_OVERFLOW_MAX - len(_ACTIVITY_OVERFLOW))
                if room:
                    _ACTIVITY_OVERFLOW.extend(batch[:room])
                if room < len(batch):
                    log.critical("Activity overflow buffer full; %s events could not be retained", len(batch) - room)
            for _ in range(queued_count):
                _ACTIVITY_QUEUE.task_done()
            _ACTIVITY_WRITER_BUSY = False

async def track_activity(message: Message) -> None:
    if not message.from_user or message.from_user.is_bot or not is_group(message):
        return
    stamp = time.monotonic()
    _RECENT_CHAT_USERS[(message.chat.id, message.from_user.id)] = (
        message.from_user.username, message.from_user.full_name, stamp,
    )
    if len(_RECENT_CHAT_USERS) > 10000:
        cutoff = stamp - _RECENT_CHAT_USERS_TTL
        for old_key, (_, _, old_stamp) in list(_RECENT_CHAT_USERS.items()):
            if old_stamp < cutoff:
                _RECENT_CHAT_USERS.pop(old_key, None)
    item = (
        message.chat.id, message.from_user.id, message.from_user.username,
        message.from_user.full_name, datetime.now(timezone.utc).isoformat(),
        message.message_id,
    )
    try:
        _ACTIVITY_QUEUE.put_nowait(item)
    except asyncio.QueueFull:
        if len(_ACTIVITY_OVERFLOW) < _ACTIVITY_OVERFLOW_MAX:
            _ACTIVITY_OVERFLOW.append(item)
            log.warning("Activity queue is full; buffered event chat=%s user=%s overflow=%s",
                        message.chat.id, message.from_user.id, len(_ACTIVITY_OVERFLOW))
        else:
            log.critical("Activity queue and overflow buffer are full; event lost chat=%s user=%s",
                         message.chat.id, message.from_user.id)



# ── Moderation: shared command implementation ─────────────────────────────────
async def moderation_target(message: Message, command: str) -> tuple[int, str] | None:
    target = await resolve_target(message, args_after_command(message))
    if not target:
        await message.answer(f"Ответьте на сообщение участника командой <code>/{command}</code>.")
        return None
    if message.from_user and target[0] == message.from_user.id:
        await message.answer("Нельзя применить это действие к самому себе.")
        return None
    if target[0] == OWNER_ID and message.from_user and message.from_user.id != OWNER_ID:
        await message.answer("⛔ Нельзя применять это действие к владельцу бота.")
        return None
    if message.from_user and target[0] != message.from_user.id:
        actor_is_admin = await is_admin(bot, message.chat.id, message.from_user.id)
        if not actor_is_admin:
            actor_rank, target_rank = await asyncio.gather(
                asyncio.to_thread(user_rank, message.chat.id, message.from_user.id),
                asyncio.to_thread(user_rank, message.chat.id, target[0]),
            )
            if target_rank >= actor_rank and target_rank > 0:
                await message.answer("⛔ Нельзя модерировать участника с равным или более высоким внутренним рангом.")
                return None
    return target

async def ensure_moderation_target_safe(
    message: Message, target: tuple[int, str], action: str, *, protect_admins: bool = True
) -> bool:
    """Fail closed for the chat creator, protected bot owner, self-actions and Telegram admins."""
    target_id, _target_name = target
    actor_id = message.from_user.id if message.from_user else 0
    if target_id == actor_id:
        await message.answer("Нельзя применить это действие к самому себе.")
        return False
    if target_id == OWNER_ID and actor_id != OWNER_ID:
        await message.answer("⛔ Нельзя применять это действие к владельцу бота.")
        return False
    try:
        member = await get_chat_member_cached(bot, message.chat.id, target_id)
    except Exception:
        log.exception("Could not verify moderation target status chat=%s target=%s action=%s",
                      message.chat.id, target_id, action)
        await message.answer("⚠️ Не удалось проверить статус участника. Ничего не применено; попробуй ещё раз.")
        return False
    if member and member.status == ChatMemberStatus.CREATOR:
        await message.answer("⛔ Нельзя применять это действие к создателю чата.")
        return False
    if protect_admins and member and member.status == ChatMemberStatus.ADMINISTRATOR:
        await message.answer("⛔ Telegram не позволяет боту применять это действие к администратору чата.")
        return False
    return True


async def check_mod(message: Message, min_rank: int = 2, require_restrict: bool = True) -> bool:
    if not await require_admin(message, min_rank=min_rank):
        return False
    if not require_restrict:
        # Internal warnings are stored by Pear CM and do not require Telegram restrict rights.
        return True
    try:
        me = await bot.get_me()
        member = await bot.get_chat_member(message.chat.id, me.id)
    except TelegramForbiddenError:
        await message.answer("⛔ Бот не имеет доступа к чату. Добавьте его обратно и выдайте права администратора.")
        return False
    except TelegramBadRequest:
        await message.answer("⛔ Не удалось проверить права бота. Убедитесь, что он состоит в чате и назначен администратором.")
        return False
    if member.status not in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR):
        await message.answer("⛔ Pear CM не администратор чата. Назначьте его администратором.")
        return False
    if member.status == ChatMemberStatus.ADMINISTRATOR and not getattr(member, "can_restrict_members", False):
        await message.answer("⛔ У Pear CM нет права «Ограничение участников». Включите его в настройках администраторов.")
        return False
    return True

@router.message(Command("mute"))
@router.message(F.text.regexp(r"(?i)^[.!/]?(?:(?:дать|дай|дайте|выдать|выдай|выдайте)\s+)?(?:мут(?:ить|а|у|ом|е|ы|ов|ами|ах)?|замут(?:ить|и|ь|ьте|ите|а|у|ом|е)?|замуч(?:ить|и|ь|ьте|ите)?|заглуш(?:ить|и|ь|ьте|ите)?)(?:\s|$)"))
@router.message(F.text.regexp(r"(?i)^@\w+\s+(?:мут(?:ить|а|у|ом|е|ы|ов|ами|ах)?|замут(?:ить|и|ь|ьте|ите|а|у|ом|е)?|замуч(?:ить|и|ь|ьте|ите)?|заглуш(?:ить|и|ь|ьте|ите)?)(?:\s|$)"))
async def mute_cmd(message: Message) -> None:
    if not await check_mod(message):
        return
    original_text = (message.text or "").strip()
    raw_args = re.split(r"(?i)^\s*[.!/]?(?:(?:дать|дай|дайте|выдать|выдай|выдайте)\s+)?(?:мут|замут(?:ить|и|ь|ьте|ите|а|у|ом|е)?|замуч(?:ить|и|ь|ьте|ите)?|заглуш(?:ить|и|ь|ьте|ите)?)\s*", original_text, maxsplit=1)[-1].split()
    if original_text.startswith("@"):
        raw_args = re.sub(r"(?i)^@\w+\s+(?:мут|замут(?:ить|и|ь|ьте|ите|а|у|ом|е)?|замуч(?:ить|и|ь|ьте|ите)?|заглуш(?:ить|и|ь|ьте|ите)?)\s*", "", original_text).split()
    target = None
    if message.reply_to_message and message.reply_to_message.from_user and not message.reply_to_message.from_user.is_bot:
        u = message.reply_to_message.from_user
        target = (u.id, u.full_name)
    elif not message.reply_to_message:
        # Prefer @username over numbers so «мут 1 час @user» never treats 1 as a user ID.
        username_match = re.search(r"@\w+", original_text)
        username_arg = username_match.group(0) if username_match else next((x for x in raw_args if x.startswith("@")), None)
        id_arg = next((x for x in raw_args if x.isdigit() and len(x) >= 7), None)
        target = await resolve_target(message, [username_arg] if username_arg else ([id_arg] if id_arg else []))
    if not target:
        await message.answer("⛔ Не вижу цель. Ответьте на сообщение участника или укажите известный чату @username/ID.")
        return
    if message.reply_to_message and message.reply_to_message.from_user and message.reply_to_message.from_user.is_bot:
        await message.answer("Ответьте именно на сообщение участника, а не на сообщение бота.")
        return
    if message.from_user and target[0] == message.from_user.id:
        await message.answer("Нельзя замутить самого себя.")
        return
    if not await ensure_moderation_target_safe(message, target, "mute"):
        return

    duration_tokens = list(raw_args)
    if not message.reply_to_message:
        duration_tokens = [x for x in duration_tokens if x != (username_arg or id_arg)]
    duration, duration_label, reason = split_duration_reason(duration_tokens)
    permanent_words = {"навсегда", "бессрочно", "навечно", "перманентно", "постоянно"}
    issued_at = moscow_now()
    if duration is None:
        if not duration_tokens or duration_tokens[0].lower().strip(",.;") in permanent_words:
            duration_label = "бессрочно"
            reason = (" ".join(duration_tokens[1:]).strip() or None) if duration_tokens else None
            until = None
        elif not any(re.match(r"^\d", token) or _unit_seconds(token) is not None for token in duration_tokens):
            duration_label = "бессрочно"
            reason = " ".join(duration_tokens).strip() or None
            until = None
        else:
            await message.answer("Не удалось распознать срок. Если срок не указан, мут бессрочный. Примеры: <code>мут час</code>, <code>мут 2 часа спам</code>, <code>мут 67 часов флуд</code>.")
            return
    else:
        until = issued_at + duration

    reason = escape(_reason_from_preset(message.chat.id, "mute", reason or "не указана"))
    actor = mention(message.from_user.id, message.from_user.full_name) if message.from_user else "модератор"
    try:
        # Telegram cannot restrict a chat creator or administrator.
        try:
            target_member = await bot.get_chat_member(message.chat.id, target[0])
            if target_member.status == ChatMemberStatus.CREATOR:
                await message.answer("⛔ Создателя чата нельзя замутить через Telegram Bot API.")
                return
            if target_member.status == ChatMemberStatus.ADMINISTRATOR:
                await message.answer("⛔ Нельзя замутить администратора чата. Сначала снимите с него права администратора.")
                return
        except TelegramBadRequest:
            # Let restrict_chat_member return the precise error for an unknown/non-member target.
            pass

        await bot.restrict_chat_member(
            chat_id=message.chat.id,
            user_id=target[0],
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
            until_date=until,
        )
        audit(message.chat.id, message.from_user.id if message.from_user else 0, "mute", target[0],
              f"duration={duration_label}; reason={reason}; moderator={message.from_user.id if message.from_user else 0}")
        if until:
            when = format_moscow_time(until)
            await message.answer(
                f"🔇 {mention(target[0], target[1])} замучен на <b>{escape(duration_label or 'указанный срок')}</b> "
                f"по причине: <b>{reason}</b>.\n"
                f"🕒 Выдан: {format_moscow_time(issued_at)}\n"
                f"⏳ Истекает: {when}\n👮 Модератор: {actor}."
            )
        else:
            await message.answer(
                f"🔇 {mention(target[0], target[1])} замучен <b>бессрочно</b> "
                f"по причине: <b>{reason}</b>.\n👮 Модератор: {actor}."
            )
    except Exception as exc:
        await safe_error(message, exc)

@router.message(Command("unmute"))
@router.message(F.text.regexp(r"(?i)^(?:[.!/]?(?:снять|сними|снимите|снимайте|убрать|убери|уберите)\s+(?:мут|мута|муту|мутом|муте|муты|мутов|мутами|мутами)|[.!/]?(?:размут(?:ить|и|ь|ьте|ите|а|у)?|анмут(?:ить|и|ь|ьте|ите)?|унмут|снятьмут|снятьмуту|убратьмут|анмутить))(?:\s|$)"))
async def unmute_cmd(message: Message) -> None:
    if not await check_mod(message):
        return
    target = await moderation_target(message, "unmute")
    if not target:
        return
    try:
        chat_info = await bot.get_chat(message.chat.id)
        default_permissions = getattr(chat_info, "permissions", None)
        if default_permissions is not None:
            permissions = ChatPermissions(**default_permissions.model_dump(exclude_none=True))
        else:
            # Telegram omits defaults only on some chat types; use the standard
            # permissive set only when no default-permissions object is available.
            permissions = ChatPermissions(
                can_send_messages=True, can_send_audios=True, can_send_documents=True,
                can_send_photos=True, can_send_videos=True, can_send_video_notes=True,
                can_send_voice_notes=True, can_send_polls=True, can_send_other_messages=True,
                can_add_web_page_previews=True,
            )
        await bot.restrict_chat_member(message.chat.id, target[0], permissions=permissions)
        audit(message.chat.id, message.from_user.id, "unmute", target[0])
        await message.answer(f"🔊 Ограничения сняты с {mention(target[0], target[1])}.")
    except Exception as exc:
        await safe_error(message, exc)

async def check_ban_operator_access(message: Message) -> bool:
    """Allow the configured owner/operators or normal chat moderators to ban/unban."""
    if not message.from_user or not is_group(message):
        await message.answer("Эта команда работает только в группе.")
        return False
    actor_id = message.from_user.id
    operator = actor_id == OWNER_ID
    if not operator:
        try:
            operator = bool(await asyncio.to_thread(_operator_active, actor_id))
        except Exception:
            operator = False
    if not operator and not await require_admin(message, min_rank=3):
        return False
    try:
        me = await bot.get_me()
        bot_member = await get_chat_member_cached(bot, message.chat.id, me.id)
    except Exception:
        await message.answer("⛔ Не удалось проверить права Pear CM в этой группе.")
        return False
    if not bot_member or bot_member.status not in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR):
        await message.answer("⛔ Назначь Pear CM администратором группы.")
        return False
    if bot_member.status == ChatMemberStatus.ADMINISTRATOR and not getattr(bot_member, "can_restrict_members", False):
        await message.answer("⛔ У Pear CM нет права «Блокировка пользователей / Ограничение участников».")
        return False
    return True


@router.message(Command("ban"))
@router.message(F.text.regexp(r"(?i)^[.!/]?(?:(?:дать|дай|дайте|выдать|выдай|выдайте)\s+)?(?:бан(?:ить|а|у|ом|е|ы|ов|ами|ах)?|забан(?:ить|ь|ьте)?|блок(?:ировать|ируй|ируйте|ни|нуть)?|заблок(?:ировать|ируй|ируйте)?)(?:\s|$)"))
async def ban_cmd(message: Message) -> None:
    if not await check_ban_operator_access(message):
        return

    raw_args = args_after_command(message)
    if message.reply_to_message:
        target = await resolve_target(message, raw_args)
        remainder = raw_args
    else:
        if not raw_args:
            await message.answer(
                "Формат: <code>/ban @username нарушение правил</code> — навсегда; "
                "<code>/ban 123456789 1 день нарушение правил</code> — временно. "
                "Можно также ответить на сообщение."
            )
            return
        target = await resolve_target(message, raw_args[:1])
        remainder = raw_args[1:]

    if not target:
        await message.answer(
            "⛔ Не удалось определить пользователя. Ответь на его сообщение, укажи Telegram ID "
            "или @username, который бот уже видел в этом чате."
        )
        return
    if not await ensure_moderation_target_safe(message, target, "ban"):
        return

    permanent_words = {
        "навсегда", "навечно", "бессрочно", "перманентно", "постоянно",
        "пожизненно", "вечный", "вечная", "вечное", "вечного", "перманентный",
    }
    duration = None
    duration_label = "навсегда"
    reason = None
    if remainder and remainder[0].lower().strip(",.;:") in permanent_words:
        reason = " ".join(remainder[1:]).strip() or None
    else:
        parsed = parse_duration_prefix(remainder)
        if parsed:
            duration, consumed, duration_label = parsed
            reason = " ".join(remainder[consumed:]).strip() or None
        else:
            looks_like_duration = any(
                re.fullmatch(r"\d+", token.strip(",.;:")) or
                _unit_seconds(token.strip(",.;:").lower()) is not None
                for token in remainder
            )
            if looks_like_duration:
                await message.answer(
                    "⛔ Не удалось распознать срок бана. Примеры: <code>/ban @user 1 день спам</code>, "
                    "<code>/ban 123456789 2 недели флуд</code>, <code>/ban @user навсегда нарушение правил</code>. "
                    "Поддерживаются секунды, минуты, часы, дни, недели, месяцы и годы."
                )
                return
            reason = " ".join(remainder).strip() or None

    reason = escape(_reason_from_preset(message.chat.id, "ban", reason or "нарушение правил")[:300])
    issued_at = moscow_now()
    until = issued_at + duration if duration else None
    actor = mention(message.from_user.id, message.from_user.full_name) if message.from_user else "модератор"
    try:
        await bot.ban_chat_member(message.chat.id, target[0], until_date=until)
        audit(
            message.chat.id, message.from_user.id if message.from_user else 0,
            "ban", target[0],
            f"duration={duration_label}; reason={reason}; moderator={message.from_user.id if message.from_user else 0}",
        )
        if until:
            await message.answer(
                f"⛔ {mention(target[0], target[1])} заблокирован на <b>{escape(duration_label)}</b>. "
                f"Причина: <b>{reason}</b>.\n"
                f"🕒 Выдан: {format_moscow_time(issued_at)}\n"
                f"⏳ Истекает: {format_moscow_time(until)}\n👮 Модератор: {actor}."
            )
        else:
            await message.answer(
                f"⛔ {mention(target[0], target[1])} заблокирован <b>навсегда</b>. "
                f"Причина: <b>{reason}</b>.\n👮 Модератор: {actor}."
            )
    except Exception as exc:
        await safe_error(message, exc)

@router.message(Command("unban"))
@router.message(F.text.regexp(r"(?i)^(?:[.!/]?разбан(?:ить|а|у|ь|ьте|ься)?|[.!/]?(?:снять|сними|снимите|снимать|убрать|убери|уберите)\s+бан(?:а|у|ом|е|ы|ов|ами|ах)?|[.!/]?анбан)(?:\s|$)"))
async def unban_cmd(message: Message) -> None:
    if not await check_ban_operator_access(message):
        return
    args = args_after_command(message)
    target = await resolve_target(message, args)
    if not target:
        await message.answer(
            "Формат: <code>/unban 123456789</code> или <code>/unban @username</code>, "
            "либо ответь на сообщение командой <code>/unban</code>. "
            "Username должен быть известен боту из этого чата."
        )
        return
    if target[0] == OWNER_ID and message.from_user and message.from_user.id != OWNER_ID:
        await message.answer("⛔ Нельзя снимать ограничения владельца бота.")
        return
    try:
        await bot.unban_chat_member(message.chat.id, target[0], only_if_banned=True)
        audit(message.chat.id, message.from_user.id if message.from_user else 0, "unban", target[0], "manual unban")
        await message.answer(f"✅ {mention(target[0], target[1])} разблокирован в этом чате.")
    except Exception as exc:
        await safe_error(message, exc)

@router.message(Command("kick"))
@router.message(F.text.regexp(r"(?i)^[.!/]?(?:(?:дать|дай|дайте|выдать|выдай|выдайте)\s+)?(?:кик(?:нуть|ни|нись|а|у|ом|е|и|ов|ами)?|исключ(?:ить|и|ите)|выгнать|выгони|выгоните|выдвор(?:ить|и|ите)|изгнать|изгони)(?:\s|$)"))
async def kick_cmd(message: Message) -> None:
    if not await check_mod(message, min_rank=3):
        return
    target = await moderation_target(message, "kick")
    if not target:
        return
    if not await ensure_moderation_target_safe(message, target, "kick"):
        return
    raw_args = args_after_command(message)
    reason = _reason_from_preset(message.chat.id, "kick", " ".join(raw_args if message.reply_to_message else raw_args[1:]).strip() or "не указана")
    try:
        await bot.ban_chat_member(message.chat.id, target[0])
        await bot.unban_chat_member(message.chat.id, target[0], only_if_banned=True)
        audit(message.chat.id, message.from_user.id, "kick", target[0], f"reason={reason}")
        await message.answer(f"👢 {mention(target[0], target[1])} исключён из чата. Причина: <b>{escape(reason[:300])}</b>.")
    except Exception as exc:
        await safe_error(message, exc)

@router.message(Command("warn"))
@router.message(F.text.regexp(r"(?i)^[.!/]?(?:(?:дать|дай|дайте|выдать|выдай|выдайте|поставить|поставь|поставьте)\s+)?(?:варн(?:ить|а|у|ом|е|ы|ов|ами)?|предупред(?:ить|и|ите)|предупреждай|предупреждайте|предупреждени(?:е|я|ю|ем|и))(?:\s|$)"))
async def warn_cmd(message: Message) -> None:
    if not await check_mod(message, require_restrict=False):
        return
    raw_args = args_after_command(message)
    target = await resolve_target(message, [] if message.reply_to_message else raw_args[:1])
    if not target:
        await message.answer("Ответьте на сообщение участника или укажите известный чату @username/ID.")
        return
    if message.from_user and target[0] == message.from_user.id:
        await message.answer("Нельзя выдать варн самому себе.")
        return
    if target[0] == OWNER_ID and message.from_user and message.from_user.id != OWNER_ID:
        await message.answer("⛔ Нельзя выдать предупреждение владельцу бота.")
        return
    if not await ensure_moderation_target_safe(message, target, "warn", protect_admins=False):
        return
    if message.from_user and target[0] != message.from_user.id and not await is_admin(bot, message.chat.id, message.from_user.id):
        actor_rank, target_rank = await asyncio.gather(
            asyncio.to_thread(user_rank, message.chat.id, message.from_user.id),
            asyncio.to_thread(user_rank, message.chat.id, target[0]),
        )
        if target_rank >= actor_rank and target_rank > 0:
            await message.answer("⛔ Нельзя выдавать предупреждения участнику с равным или более высоким внутренним рангом.")
            return
    actor_id = message.from_user.id if message.from_user else 0
    warn_key = (message.chat.id, actor_id, target[0])
    now_mono = time.monotonic()
    previous_warn = _WARN_RECENT.get(warn_key, 0.0)
    if now_mono - previous_warn < 3.0:
        await message.answer("ℹ️ Похожая команда варна уже обработана. Подожди несколько секунд, чтобы не выдать предупреждение дважды.")
        return
    _WARN_RECENT[warn_key] = now_mono
    if len(_WARN_RECENT) > 5000:
        for key, stamp in list(_WARN_RECENT.items()):
            if now_mono - stamp > 60:
                _WARN_RECENT.pop(key, None)
    reason = _reason_from_preset(message.chat.id, "warn", " ".join(raw_args if message.reply_to_message else raw_args[1:]).strip() or "не указана")
    # Each warning expires after seven days.
    # Fetch the current warning ledger and user row concurrently; neither depends on the other.
    before, user = await asyncio.gather(
        asyncio.to_thread(active_warn_count, message.chat.id, target[0]),
        asyncio.to_thread(db_one, "bot_users", user_id=target[0], chat_id=message.chat.id),
    )
    user = user or {}
    # The audit ledger is the source of truth for seven-day warning expiry/counts.
    # Persist it first; never announce a warning if the ledger write failed.
    audit_saved = await audit_wait(
        message.chat.id, message.from_user.id if message.from_user else 0,
        "warn", target[0], f"reason={reason}; expires_in_days=7",
    )
    if not audit_saved:
        await message.answer("⚠️ Варн не зарегистрирован: база не подтвердила запись в журнал. Попробуй позже.")
        return
    count = before + 1
    saved = await asyncio.to_thread(db_upsert, "bot_users", {
        **user, "user_id": target[0], "chat_id": message.chat.id,
        "warns": count, "rank": int(user.get("rank") or 0),
        "messages": int(user.get("messages") or 0),
    }, "user_id,chat_id")
    if not saved:
        log.error("Warning audit saved but profile counter update failed chat=%s user=%s", message.chat.id, target[0])
    actor = mention(message.from_user.id, message.from_user.full_name) if message.from_user else "модератор"
    profile_note = "" if saved else "\n⚠️ Журнал сохранён, но счётчик профиля не обновился. Не повторяй команду."
    await message.answer(
        f"⚠️ {mention(target[0], target[1])} получил предупреждение <b>{count}/3</b>. "
        f"Причина: <b>{escape(reason[:300])}</b>.\n👮 Модератор: {actor}. "
        f"Варн действует 7 дней.{profile_note}"
    )
    # Three active warnings trigger a ban. This is intentionally a single threshold
    # to avoid multiple penalties firing for the same warning.
    if before < 3 <= count:
        try:
            target_member = await bot.get_chat_member(message.chat.id, target[0])
            if target_member.status in (ChatMemberStatus.CREATOR, ChatMemberStatus.ADMINISTRATOR):
                await message.answer("⚠️ Достигнут лимит варнов, но Telegram не позволяет банить администратора/создателя.")
                return
            await bot.ban_chat_member(message.chat.id, target[0])
            audit(message.chat.id, message.from_user.id if message.from_user else 0, "auto_ban", target[0],
                  f"3 active warnings in 7 days; reason={reason}")
            await message.answer(
                f"⛔ {mention(target[0], target[1])} забанен автоматически: 3 предупреждения за последние 7 дней."
            )
        except Exception as exc:
            await safe_error(message, exc)

@router.message(Command("unwarn"))
@router.message(F.text.regexp(r"(?i)^(?:[.!/]?(?:снять|сними|снимите|снимайте|убрать|убери|уберите|удалить|удали|удалите)\s+(?:варн|варна|варну|варном|предупреждени(?:е|я|ю|ем|и))|[.!/]?(?:анварн|снятьварн|убратьварн|разварн|аннулироватьварн|снятьпредупреждение|убратьпредупреждение))(?:\s|$)"))
async def unwarn_cmd(message: Message) -> None:
    if not await require_admin(message, min_rank=2):
        return
    target = await moderation_target(message, "unwarn")
    if not target:
        return
    if not await ensure_moderation_target_safe(message, target, "unwarn"):
        return
    count = await asyncio.to_thread(active_warn_count, message.chat.id, target[0])
    if count <= 0:
        await message.answer(f"ℹ️ У {mention(target[0], target[1])} нет действующих предупреждений за последние 7 дней.")
        return
    new_count = max(0, count - 1)
    ledger_saved = await audit_wait(message.chat.id, message.from_user.id if message.from_user else 0, "unwarn", target[0], f"warns={new_count}")
    if not ledger_saved:
        await message.answer("⚠️ Не удалось записать снятие предупреждения в журнал. Счётчик не меняю — попробуй позже.")
        return
    user = await asyncio.to_thread(db_one, "bot_users", user_id=target[0], chat_id=message.chat.id) or {}
    saved = await asyncio.to_thread(db_upsert, "bot_users", {
        **user, "user_id": target[0], "chat_id": message.chat.id,
        "warns": new_count, "rank": int(user.get("rank", 0) or 0), "messages": int(user.get("messages", 0) or 0),
    }, "user_id,chat_id")
    if not saved:
        await message.answer("⚠️ Снятие зарегистрировано в журнале, но счётчик профиля не обновился. Не повторяй команду; проверь базу.")
        return
    await message.answer(f"✅ Снято одно действующее предупреждение у {mention(target[0], target[1])}. Осталось: <b>{new_count}</b>.")
    
@router.message(Command("clearwarns"))
@router.message(F.text.regexp(r"(?i)^[.!/]?(?:сбросварнов|обнулитьварны)(?:\s|$)"))
async def clearwarns_cmd(message: Message) -> None:
    if not await require_admin(message, min_rank=3):
        return
    target = await moderation_target(message, "clearwarns")
    if not target:
        return
    if not await ensure_moderation_target_safe(message, target, "clearwarns"):
        return
    ledger_saved = await audit_wait(message.chat.id, message.from_user.id if message.from_user else 0, "clearwarns", target[0])
    if not ledger_saved:
        await message.answer("⚠️ Не удалось записать сброс предупреждений в журнал. Данные не подтверждаю — попробуй позже.")
        return
    user = await asyncio.to_thread(db_one, "bot_users", user_id=target[0], chat_id=message.chat.id) or {}
    saved = await asyncio.to_thread(db_upsert, "bot_users", {
        **user, "user_id": target[0], "chat_id": message.chat.id,
        "warns": 0, "rank": int(user.get("rank", 0) or 0), "messages": int(user.get("messages", 0) or 0),
    }, "user_id,chat_id")
    if not saved:
        await message.answer("⚠️ Сброс записан в журнале, но счётчик профиля не обновился. Не повторяй команду; проверь базу.")
        return
    await message.answer(f"🧹 Предупреждения сброшены для {mention(target[0], target[1])}.")

# ── Message tools ─────────────────────────────────────────────────────────────
@router.message(Command("del"))
@router.message(F.text.regexp(r"(?i)^\s*-смс(?:\s+\d+)?\s*$"))
async def delete_message_cmd(message: Message) -> None:
    if not await require_admin(message):
        return
    raw = (message.text or "").strip()
    match = re.fullmatch(r"(?i)\s*-смс(?:\s+(\d+))?\s*", raw)
    if match and match.group(1):
        requested = int(match.group(1))
        if requested < 1:
            await message.answer("Укажи число от 1 до 100: <code>-смс 10</code>.")
            return
        if requested > 100:
            try:
                await message.delete()
            except Exception:
                pass
            await message.answer("⚠️ За один раз можно удалить максимум <b>100 сообщений</b>. Используй: <code>-смс 100</code>.")
            return

        count = max(1, requested)
        # The requested limit includes the command message itself. Delete count-1
        # preceding messages, then delete this command, for a true maximum of count.
        previous_count = max(0, count - 1)
        first_id = max(1, message.message_id - previous_count)
        observed = [
            mid for mid in _RECENT_MESSAGE_IDS.get(message.chat.id, ())
            if first_id <= mid < message.message_id
        ]
        candidates = list(dict.fromkeys(reversed(observed)))[:previous_count]
        candidate_set = set(candidates)
        if len(candidates) < previous_count:
            for mid in range(message.message_id - 1, first_id - 1, -1):
                if mid not in candidate_set:
                    candidates.append(mid)
                    candidate_set.add(mid)
                    if len(candidates) >= previous_count:
                        break
        forbidden = False
        individually_deleted = 0
        semaphore = asyncio.Semaphore(5)

        async def delete_one(mid: int) -> bool:
            nonlocal forbidden
            async with semaphore:
                try:
                    await bot.delete_message(chat_id=message.chat.id, message_id=mid)
                    return True
                except TelegramRetryAfter as retry:
                    await asyncio.sleep(float(retry.retry_after) + 0.2)
                    try:
                        await bot.delete_message(chat_id=message.chat.id, message_id=mid)
                        return True
                    except TelegramForbiddenError:
                        forbidden = True
                        return False
                    except TelegramBadRequest as inner_exc:
                        log.debug("Skipped chat=%s message=%s: %s", message.chat.id, mid, inner_exc)
                        return False
                    except Exception as inner_exc:
                        log.debug("Could not delete chat=%s message=%s: %s", message.chat.id, mid, inner_exc)
                        return False
                except TelegramForbiddenError:
                    forbidden = True
                    return False
                except TelegramBadRequest as inner_exc:
                    # Missing/old/service messages are expected and must not abort the purge.
                    log.debug("Skipped chat=%s message=%s: %s", message.chat.id, mid, inner_exc)
                    return False
                except Exception as inner_exc:
                    log.debug("Could not delete chat=%s message=%s: %s", message.chat.id, mid, inner_exc)
                    return False

        # Delete individually so the reply can report an actual count. Telegram's
        # bulk delete endpoint returns only True/False and can silently skip IDs.
        results = await asyncio.gather(*(delete_one(mid) for mid in candidates))
        individually_deleted = sum(1 for result in results if result)
        # Count the command itself only if Telegram confirmed its deletion.
        command_deleted = False
        try:
            await message.delete()
            command_deleted = True
        except Exception as exc:
            log.debug("Could not delete purge command chat=%s message=%s: %s", message.chat.id, message.message_id, exc)
        deleted_total = individually_deleted + int(command_deleted)

        audit(
            message.chat.id, message.from_user.id, "purge_recent",
            details=f"requested={count};first_id={first_id};last_id={message.message_id - 1};deleted={deleted_total}",
        )
        if forbidden:
            await message.answer("⛔ У бота нет права «Удаление сообщений». Выдайте это право в настройках группы.")
        else:
            await message.answer(
                f"🧹 Удалено сообщений: <b>{deleted_total}</b> из последних {count}."
                "\nУже удалённые, старые и служебные сообщения могут не учитываться."
            )
        return

    target_message = message.reply_to_message
    if not target_message:
        await message.answer("Ответьте на сообщение командой <code>/del</code> или напишите <code>-смс 10</code>.")
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
    user = await asyncio.to_thread(db_one, "bot_users", user_id=target[0], chat_id=message.chat.id) or {}
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

# Snapshot from the user's Iris leaderboard (14 Oct 2026, chat XVII).
# The observed Pear CM counters at that moment are subtracted, so only newer messages
# are added on top of Iris' historical totals.
_IRIS_BASELINE_TOTAL = 215531
_PEAR_TOTAL_AT_BASELINE = 2452
_IRIS_BASELINE_USERS = {
    "tgvkq": ("XVII хирейнз", 38412),
    "attakill": ("мади ювелир", 16443),
    "kobwq": ("кобяк патапим", 15557),
    "osadaq": ("алоени", 15280),
    "roseov": ("росеов расколов", 10571),
    "rypyi": ("awesome", 6562),
    "matteoxaxa": ("mattoslav", 6462),
    "opekcx": ("fyrtonovskyy", 5257),
    "proletarov": ("алеша", 4874),
    "ocvsq": ("Young", 3990),
    "bleeg": ("фифти сент", 3619),
    "oxcqw": ("сабвейка", 3579),
    "tgiwq": ("килька хирейнз", 3373),
    "moxvd": ("визи хирейнз  NomerokAppBot", 3201),
    "shqume": ("Shqume", 2878),
    "qxhza": ("Pepel//Archangelov", 2826),
    "qwzqi": ("Кузя", 2360),
    "heroxec": ("overlord", 2153),
    "unfargettable": ("What Crime", 2039),
    "smolderbody": ("spleen", 1947),
    "shefkb": ("шеф", 1893),
    "ispumpkin": ("Царек", 1843),
    "qvmmq": ("insomniac", 1823),
    "pidaxi": ("кирилл", 1364),
    "texwu": ("XVII малой", 1357),
    "alcapone_work": ("XVII ALCapone eskizz", 1321),
    "whyflipper": ("кирч", 1321),
    "skaming1337": ("God", 1281),
    "wolterenko": ("Егоркин-Ю", 1263),
    "oxuest": ("oxuest", 1233),
}
# Pear CM all-time counts visible in the same snapshot; used as a one-time offset.
_PEAR_USER_COUNTS_AT_BASELINE = {
    "tgvkq": 770, "rypyi": 518, "roseov": 308, "kobwq": 169, "oxcqw": 144,
    "bleeg": 120, "osadaq": 110, "attakill": 101, "qxhza": 66, "fxguf": 56,
    "vbnk": 25, "ispumpkin": 12, "qseeis": 9, "ocvsq": 7, "cerry1n": 7,
    "whnix": 6, "mercifley": 5, "wqsir": 4, "pidaxi": 4, "m1santhr0pic": 3,
    "tgiwq": 3, "qwzqi": 1, "oemqx": 1, "femxl": 1,
}

@router.message(Command("top", "stats", "stat"))
@router.message(F.text.regexp(r"(?i)^[.!/]?(?:топ|стата|статистика)(?:\s|$)"))
async def top_cmd(message: Message) -> None:
    raw = (message.text or "").strip().lower()
    arg = re.sub(r"^(?:/(?:top|stats|stat)(?:@\w+)?|[.!/]?(?:топ|стата|статистика))\s*", "", raw, flags=re.I).strip().replace("ё", "е")
    category = re.sub(r"\s+", " ", arg).strip()
    category_aliases = {
        "деньги":"money", "баланс":"money", "валюта":"money", "по деньгам":"money", "по балансу":"money",
        "ранги":"rank", "по рангам":"rank", "репутация":"rep", "по репутации":"rep",
        "отношения":"rep", "по отношениям":"rep", "браки":"marriages", "по бракам":"marriages",
        "кланы":"clans", "по кланам":"clans", "награды":"awards", "по наградам":"awards",
        "варны":"warns", "предупреждения":"warns", "по варнам":"warns",
        "сообщения":"messages", "сообщений":"messages", "по сообщениям":"messages", "активность":"messages",
        "участники":"members", "новички":"members", "по участникам":"members",
    }
    category_key = category_aliases.get(category)
    if category_key:
        table_map = {"money":("bot_economy","balance","🪙"), "rank":("bot_users","rank","⭐"),
                     "rep":("bot_relationships","points","❤️"), "marriages":("bot_marriages","id","💍"),
                     "clans":("bot_clans","id","🏰"), "awards":("bot_awards","id","🏅"),
                     "warns":("bot_users","warns","⚠️")}
        if category_key == "marriages":
            rows = await asyncio.to_thread(db_rows, "bot_marriages", 1000, None, chat_id=message.chat.id, status="married")
            pairs = [r for r in rows if r.get("status") == "married"]
            await message.answer("<b>🏆 Топ браков</b>\n" + ("\n".join(f"{i}. {escape(str(p.get('user1_name','Участник')))} ❤️ {escape(str(p.get('user2_name','Участник')))}" for i,p in enumerate(pairs[:10],1)) if pairs else "Пока нет зарегистрированных браков."))
            return
        if category_key == "clans":
            rows = await asyncio.to_thread(db_rows, "bot_clans", 1000, None, chat_id=message.chat.id)
            member_rows = await asyncio.to_thread(db_rows, "bot_clan_members", 5000, None, chat_id=message.chat.id)
            counts = {}
            for member in member_rows: counts[member.get("clan_name")] = counts.get(member.get("clan_name"), 0) + 1
            rows.sort(key=lambda r: counts.get(r.get("name"), 0), reverse=True)
            lines = [f"{i}. 🏰 {escape(str(r.get('name','Клан')))} — {counts.get(r.get('name'),0)} участн." for i,r in enumerate(rows[:10],1)]
            await message.answer("<b>🏆 Топ кланов</b>\n" + ("\n".join(lines) if lines else "Пока нет кланов."))
            return
        if category_key == "members":
            rows = await asyncio.to_thread(db_rows, "bot_users", 1000, "created_at", chat_id=message.chat.id)
            rows.sort(key=lambda r: str(r.get("created_at") or ""), reverse=True)
            lines = [f"{i}. {profile_link(int(r.get('user_id') or 0), '@'+str(r.get('username')).lstrip('@') if r.get('username') else str(r.get('first_name') or 'Участник'))}" for i,r in enumerate(rows[:10],1) if int(r.get('user_id') or 0) > 0]
            await message.answer("<b>🆕 Топ новичков</b>\n" + ("\n".join(lines) if lines else "Пока нет участников."))
            return
        if category_key == "messages":
            rows = await asyncio.to_thread(db_rows, "bot_users", 1000, "messages", chat_id=message.chat.id)
            lines = [f"{i}. {profile_link(int(r.get('user_id') or 0), '@'+str(r.get('username')).lstrip('@') if r.get('username') else str(r.get('first_name') or 'Участник'))} — <b>{int(r.get('messages') or 0)}</b> 💬" for i,r in enumerate(rows[:10],1) if int(r.get('user_id') or 0) > 0]
            await message.answer("<b>🏆 Топ сообщений</b>\n" + ("\n".join(lines) if lines else "Пока нет статистики."))
            return
        table, field, icon = table_map[category_key]
        rows = await asyncio.to_thread(db_rows, table, 1000, field, chat_id=message.chat.id)
        grouped = {}
        if category_key == "rep":
            for row in rows:
                score = int(row.get("points") or 0)
                for uid in (int(row.get("user1_id") or 0), int(row.get("user2_id") or 0)):
                    if uid <= 0: continue
                    current = grouped.setdefault(uid, {"user_id":uid,"username":None,"first_name":f"ID {uid}","score":0})
                    current["score"] += score
        elif category_key == "awards":
            for row in rows:
                uid = int(row.get("user_id") or 0)
                if uid <= 0: continue
                current = grouped.setdefault(uid, {"user_id":uid,"username":None,"first_name":f"ID {uid}","score":0})
                current["score"] += 1
        else:
            for row in rows:
                uid = int(row.get("user_id") or 0)
                if uid <= 0:
                    continue
                current = grouped.setdefault(uid, {"user_id":uid,"username":row.get("username"),"first_name":row.get("first_name") or f"ID {uid}","score":0})
                current["score"] += int(row.get(field) or 0)
                if row.get("username"): current["username"] = row["username"]
        user_rows = await asyncio.to_thread(db_rows, "bot_users", 1000, "last_seen", chat_id=message.chat.id)
        user_map = {int(u.get("user_id") or 0):u for u in user_rows}
        for uid, current in grouped.items():
            u = user_map.get(uid, {})
            if u.get("username"): current["username"] = u["username"]
            if u.get("first_name"): current["first_name"] = u["first_name"]
        ranked = sorted(grouped.values(), key=lambda x:x["score"], reverse=True)[:10]
        title = {"money":"баланса","rank":"рангов","rep":"отношений","awards":"наград","warns":"предупреждений"}.get(category_key,category_key)
        await message.answer("<b>🏆 Топ " + title + "</b>\n" + ("\n".join(f"{i}. {mention(int(r['user_id']), '@'+str(r['username']).lstrip('@') if r.get('username') else str(r['first_name']))} — <b>{r['score']}</b> {icon}" for i,r in enumerate(ranked,1) if int(r.get('user_id') or 0) > 0) if ranked else "Пока нет данных для рейтинга."))
        return
    # Iris-style statistics support both "топ" and "стата", with an optional count.
    count = 30
    count_match = re.match(r"^(\d+)\s+(.+)$", arg)
    if count_match:
        count = max(1, min(100, int(count_match.group(1))))
        arg = count_match.group(2).strip()
    else:
        trailing_count = re.match(r"^(.+?)\s+(\d+)$", arg)
        if trailing_count:
            arg = trailing_count.group(1).strip()
            count = max(1, min(100, int(trailing_count.group(2))))

    global_scope = arg.startswith(("все чаты", "всех чатов", "по всем чатам", "глобальный", "глобально"))
    chat_only = is_group(message) and not global_scope
    if global_scope:
        arg = re.sub(r"^(?:все чаты|всех чатов|по всем чатам|глобальный(?:\s+топ)?|глобально)\s*", "", arg).strip()
    elif arg.startswith(("чат ", "чата ", "в чате ", "по чату ")):
        chat_only = True
        arg = re.sub(r"^(?:чат|чата|в чате|по чату)\s*", "", arg).strip()
    aliases = {
        "день":"day", "дня":"day", "дню":"day", "дне":"day", "днем":"day", "днём":"day", "дневной":"day",
        "сегодня":"day", "сегодняшний":"day", "сегодняшняя":"day", "за сегодня":"day", "за день":"day",
        "топ дня":"day", "топ день":"day", "топ за день":"day", "топ сегодня":"day", "топ за сегодня":"day",
        "сутки":"day", "суток":"day", "сутками":"day", "суточный":"day", "за сутки":"day", "топ сутки":"day", "24 часа":"day",
        "неделя":"week", "неделю":"week", "недели":"week", "неделе":"week", "неделей":"week",
        "недельный":"week", "недельная":"week", "за неделю":"week", "нед":"week", "7 дней":"week",
        "топ недели":"week", "топ неделю":"week", "топ неделя":"week", "топ за неделю":"week", "топ недельный":"week",
        "месяц":"month", "месяца":"month", "месяцу":"month", "месяце":"month", "месяцем":"month",
        "месячный":"month", "месячная":"month", "за месяц":"month", "30 дней":"month",
        "топ месяца":"month", "топ месяц":"month", "топ за месяц":"month", "топ месячный":"month",
        "год":"year", "года":"year", "году":"year", "годом":"year", "годовой":"year",
        "за год":"year", "365 дней":"year", "топ года":"year", "топ год":"year", "топ за год":"year",
        "вся":"all", "все":"all", "все время":"all", "все времена":"all", "за все время":"all",
        "топ вся":"all", "топ все":"all", "топ все время":"all", "топ все времена":"all",
        "стата вся":"all", "стата все":"all", "стата все время":"all", "статистика":"all",
        "навсегда":"all", "общий":"all", "общий топ":"all", "в целом":"all",
        "все чаты":"all", "всех чатов":"all", "глобально":"all", "глобальный":"all",
        "all":"all", "alltime":"all", "day":"day", "week":"week", "month":"month", "year":"year",
    }
    compact = re.sub(r"\s+", " ", arg).strip(" .,!?:;").replace("ё", "е")
    normalized = re.sub(r"^(?:за|топ|стата|статистика|топ за|стата за)\s+", "", compact).strip()
    period = aliases.get(compact, aliases.get(normalized, "all"))
    labels = {
        "all":"за всё время", "day":"за сутки", "week":"за неделю",
        "month":"за месяц", "year":"за год",
    }

    def query_top() -> tuple[list[dict], int]:
        if period == "all":
            # All-time counters are maintained per chat as messages arrive.
            if db is None:
                return [], 0
            q = db.table("bot_users").select("user_id,username,first_name,messages,chat_id")
            if chat_only:
                q = q.eq("chat_id", message.chat.id)
            records: list[dict] = []
            offset = 0
            while True:
                page = q.order("messages", desc=True).range(offset, offset + 999).execute().data or []
                records.extend(page)
                if len(page) < 1000:
                    break
                offset += len(page)
                if offset >= 100000:
                    log.warning("All-time leaderboard capped at 100000 user-chat rows")
                    break
            grouped: dict[int, dict] = {}
            total_messages = 0
            for row in records:
                try:
                    uid, n = int(row.get("user_id") or 0), max(0, int(row.get("messages") or 0))
                except (TypeError, ValueError):
                    continue
                if uid <= 0:
                    continue
                total_messages += n
                item = grouped.setdefault(uid, {
                    "user_id": uid, "username": row.get("username"),
                    "first_name": row.get("first_name") or "Участник", "messages": 0,
                })
                item["messages"] += n
                if row.get("username"):
                    item["username"] = row["username"]
                if row.get("first_name"):
                    item["first_name"] = row["first_name"]

            # Activate the imported Iris baseline only in the intended chat, identified
            # by several known members. This avoids changing every other group.
            if chat_only:
                seen_base_users = {
                    str(item.get("username") or "").lstrip("@").casefold()
                    for item in grouped.values()
                    if str(item.get("username") or "").lstrip("@").casefold() in _IRIS_BASELINE_USERS
                }
                if len(seen_base_users) >= 5:
                    current_by_username = {
                        str(item.get("username") or "").lstrip("@").casefold(): item
                        for item in grouped.values() if item.get("username")
                    }
                    seeded: dict[str, dict] = {}
                    consumed_user_ids: set[int] = set()
                    for uname, (base_name, base_count) in _IRIS_BASELINE_USERS.items():
                        current = current_by_username.get(uname, {})
                        uid = int(current.get("user_id") or 0)
                        observed = int(current.get("messages") or 0)
                        offset = int(_PEAR_USER_COUNTS_AT_BASELINE.get(uname, 0))
                        delta = max(0, observed - offset)
                        seeded[uname] = {
                            "user_id": uid,
                            "username": uname,
                            "first_name": current.get("first_name") or base_name,
                            "baseline_name": base_name,
                            "messages": int(base_count) + delta,
                        }
                        if uid:
                            consumed_user_ids.add(uid)
                    for uid, current in grouped.items():
                        uname = str(current.get("username") or "").lstrip("@").casefold()
                        if uname in _IRIS_BASELINE_USERS:
                            continue
                        observed = int(current.get("messages") or 0)
                        offset = int(_PEAR_USER_COUNTS_AT_BASELINE.get(uname, 0))
                        delta = max(0, observed - offset)
                        if delta > 0:
                            seeded[uname or f"id:{uid}"] = {
                                **current, "messages": delta,
                            }
                    grouped_rows = list(seeded.values())
                    total_messages = _IRIS_BASELINE_TOTAL + max(0, total_messages - _PEAR_TOTAL_AT_BASELINE)
                    return sorted(grouped_rows, key=lambda r: int(r.get("messages") or 0), reverse=True)[:count], total_messages

            return sorted(grouped.values(), key=lambda r: int(r.get("messages") or 0), reverse=True)[:count], total_messages

        now_local = moscow_now()
        if period == "day":
            period_start = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
        elif period == "week":
            period_start = (now_local - timedelta(days=now_local.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
        elif period == "month":
            period_start = now_local.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        else:
            period_start = now_local.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
        start_day = period_start.date().isoformat()
        end_day = now_local.date().isoformat()
        grouped: dict[int, dict] = {}
        total_messages = 0

        if db is None:
            return [], 0

        # Fast path: compact daily aggregates, not a scan of every message.
        try:
            q = db.table("bot_activity_daily").select("user_id,username,first_name,chat_id,messages").gte("day", start_day).lte("day", end_day)
            if chat_only:
                q = q.eq("chat_id", message.chat.id)
            offset = 0
            while True:
                page = q.order("day").order("chat_id").order("user_id").range(offset, offset + 999).execute().data or []
                for row in page:
                    try:
                        uid, n = int(row.get("user_id") or 0), max(0, int(row.get("messages") or 0))
                    except (TypeError, ValueError):
                        continue
                    if uid <= 0:
                        continue
                    total_messages += n
                    item = grouped.setdefault(uid, {
                        "user_id": uid, "username": row.get("username"),
                        "first_name": row.get("first_name") or "Участник", "messages": 0,
                    })
                    item["messages"] += n
                    if row.get("username"):
                        item["username"] = row["username"]
                    if row.get("first_name"):
                        item["first_name"] = row["first_name"]
                if len(page) < 1000:
                    break
                offset += len(page)
                if offset >= 100000:
                    log.warning("Period leaderboard capped at 100000 daily rows")
                    break
        except Exception:
            # Backward-compatible fallback if the aggregate migration has not been applied.
            log.exception("Daily aggregate unavailable; falling back to raw activity events")
            grouped.clear()
            total_messages = 0
            q = db.table("bot_activity_events").select("user_id,username,first_name,chat_id,created_at").gte("created_at", period_start.astimezone(timezone.utc).isoformat())
            if chat_only:
                q = q.eq("chat_id", message.chat.id)
            offset = 0
            while True:
                page = q.order("created_at").range(offset, offset + 999).execute().data or []
                for event in page:
                    try:
                        uid = int(event.get("user_id") or 0)
                    except (TypeError, ValueError):
                        continue
                    if uid <= 0:
                        continue
                    total_messages += 1
                    item = grouped.setdefault(uid, {
                        "user_id": uid, "username": event.get("username"),
                        "first_name": event.get("first_name") or "Участник", "messages": 0,
                    })
                    item["messages"] += 1
                    if event.get("username"):
                        item["username"] = event["username"]
                    if event.get("first_name"):
                        item["first_name"] = event["first_name"]
                if len(page) < 1000:
                    break
                offset += len(page)
                if offset >= 100000:
                    log.warning("Raw activity fallback capped at 100000 events")
                    break
        return sorted(grouped.values(), key=lambda r: int(r.get("messages") or 0), reverse=True)[:count], total_messages

    try:
        rows, total_messages = await asyncio.to_thread(query_top)
    except Exception:
        log.exception("Leaderboard query failed")
        await message.answer("⚠️ Не удалось получить статистику. Проверь подключение Supabase и таблицы активности.")
        return
    if not rows:
        await message.answer(f"📊 Пока нет данных для статистики {labels[period]}. Сообщения учитываются с момента, когда Pear CM начал наблюдать чат.")
        return

    # Display names (not usernames) are clickable profile links, like Iris.
    profile_names: dict[int, str] = {}
    if db is not None and chat_only:
        try:
            profiles = await asyncio.to_thread(
                lambda: db.table("bot_member_profiles").select("user_id,display_name")
                .eq("chat_id", message.chat.id).range(0, 999).execute().data or []
            )
            profile_names = {
                int(p.get("user_id") or 0): str(p.get("display_name") or "").strip()
                for p in profiles if str(p.get("display_name") or "").strip()
            }
        except Exception:
            # Existing stats remain available if the optional nickname/profile table is unavailable.
            profile_names = {}
    lines = [f"📊 <b>Статистика по общительным пользователям {labels[period]}</b>", ""]
    for index, row in enumerate(rows, 1):
        uid = int(row.get("user_id") or 0)
        username = str(row.get("username") or "").lstrip("@")
        # Preserve Iris' historical display names for imported baseline users unless
        # the member has explicitly set a custom +ник nickname in this chat.
        display_name = (
            profile_names.get(uid)
            or str(row.get("baseline_name") or "").strip()
            or str(row.get("first_name") or "Участник")
        )
        safe_name = escape(display_name)
        if uid > 0:
            display = profile_link(uid, display_name)
        elif username:
            display = f'<a href="https://t.me/{escape(username, quote=True)}">{safe_name}</a>'
        else:
            display = safe_name
        if period == "month":
            badge = "🔥🔥🔥 " if index <= 5 else ("💥💥 " if index <= 11 else ("⭐️ " if index <= 22 else "⚡️ "))
        elif period == "week":
            badge = "⚡️ " if index <= 2 else ("✨ " if index == 3 else "")
        else:
            badge = ""
        lines.append(f"{badge}{index}. {display} — {int(row.get('messages') or 0):,}".replace(",", " "))
    lines.extend(["", f"Всего сообщений: {total_messages:,}".replace(",", " ")])
    # Telegram message limit is 4096 chars; split defensively.
    chunks: list[str] = []
    current = ""
    for line in lines:
        candidate = (current + "\n" + line) if current else line
        if len(candidate) > 3900 and current:
            chunks.append(current)
            current = line
        else:
            current = candidate
    if current:
        chunks.append(current)
    for chunk in chunks:
        await message.answer(chunk)




@router.message(F.text.regexp(r"(?i)^\s*\+ник(?:\s|$)"))
@router.message(F.text.regexp(r"(?i)^\s*/nick(?:\s|$)"))
async def set_chat_nickname_cmd(message: Message) -> None:
    if not is_group(message):
        await message.answer("Никнеймы можно задавать внутри группы.")
        return
    if not message.from_user:
        return
    raw = (message.text or "").strip()
    match = re.match(r"(?is)^(?:\+ник|/nick)(?:@\w+)?(?:\s+(.+))?$", raw)
    nickname = (match.group(1) or "").strip() if match else ""
    target_user = message.reply_to_message.from_user if message.reply_to_message else message.from_user
    if not target_user or target_user.is_bot:
        await message.answer("Не получилось определить участника. Ответь на сообщение человека.")
        return
    if target_user.id != message.from_user.id and not await is_admin(bot, message.chat.id, message.from_user.id):
        await message.answer("Чужой ник может менять только администратор. Для своего ника напиши <code>+ник ТВОЙ НИК</code>.")
        return
    if not nickname:
        await message.answer("Укажи ник: <code>+ник АВЕСОМ</code>. Чтобы убрать его — <code>-ник</code>.")
        return
    if len(nickname) > 64:
        await message.answer("Ник слишком длинный: максимум 64 символа.")
        return
    if nickname.startswith("@"):
        await message.answer("Укажи отображаемый ник, а не @username. Например: <code>+ник АВЕСОМ</code>.")
        return
    current = await asyncio.to_thread(
        db_one, "bot_member_profiles", chat_id=message.chat.id, user_id=target_user.id
    ) or {}
    saved = await asyncio.to_thread(db_upsert, "bot_member_profiles", {
        **current, "chat_id": message.chat.id, "user_id": target_user.id,
        "display_name": nickname, "updated_at": datetime.now(timezone.utc).isoformat(),
    }, "chat_id,user_id")
    if not saved:
        await message.answer("Не удалось сохранить ник. Проверь Supabase и выполни актуальный schema.sql.")
        return
    await message.answer(f"✅ Ник участника изменён на <b>{escape(nickname)}</b>. Он будет показываться в статистике и профиле чата.")

@router.message(F.text.regexp(r"(?i)^\s*(?:-ник|/unnick)(?:\s|$)"))
async def clear_chat_nickname_cmd(message: Message) -> None:
    if not is_group(message):
        await message.answer("Никнеймы можно убирать внутри группы.")
        return
    if not message.from_user:
        return
    target_user = message.reply_to_message.from_user if message.reply_to_message else message.from_user
    if not target_user or target_user.is_bot:
        await message.answer("Не получилось определить участника. Ответь на сообщение человека.")
        return
    if target_user.id != message.from_user.id and not await is_admin(bot, message.chat.id, message.from_user.id):
        await message.answer("Чужой ник может убрать только администратор.")
        return
    current = await asyncio.to_thread(
        db_one, "bot_member_profiles", chat_id=message.chat.id, user_id=target_user.id
    ) or {}
    if not str(current.get("display_name") or "").strip():
        await message.answer("У участника нет установленного ника.")
        return
    saved = await asyncio.to_thread(db_upsert, "bot_member_profiles", {
        **current, "chat_id": message.chat.id, "user_id": target_user.id,
        "display_name": "", "updated_at": datetime.now(timezone.utc).isoformat(),
    }, "chat_id,user_id")
    await message.answer("♻️ Ник сброшен. Теперь в статистике будет отображаться имя из Telegram." if saved else "Не удалось убрать ник. Проверь Supabase.")

@router.message(Command("logs"))
@router.message(F.text.regexp(r"(?i)^[.!/]?логи(?:\s|$)"))
async def logs_cmd(message: Message) -> None:
    # In private chat, allow the configured owner to inspect global audit logs.
    if message.chat.type == ChatType.PRIVATE and message.from_user and message.from_user.id == OWNER_ID:
        rows = await asyncio.to_thread(db_rows, "bot_audit", limit=15, order="created_at")
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
    rows = await asyncio.to_thread(db_rows, "bot_audit", limit=10, order="created_at", chat_id=message.chat.id)
    if not rows:
        await message.answer("📒 Логов пока нет.")
        return
    lines = ["📒 <b>Последние действия</b>"]
    for row in rows:
        stamp = (row.get("created_at") or "")[:16].replace("T", " ")
        lines.append(f"• <code>{stamp}</code> — {row.get('action', '?')} → <code>{row.get('target_id') or '—'}</code> {row.get('details') or ''}")
    await message.answer("\n".join(lines))

# ── Simple marriage feature ───────────────────────────────────────────────────
@router.message(Command("marry", "proposemarriage", "wed"))
@router.message(F.text.regexp(r"(?i)^\s*[.!/]?(?:брак|браки|брачок|брачиться|пожениться|поженимся|жениться|выходи замуж|выйти замуж|предложить брак|кинуть брак|заключить брак|свадьба|женитьба)\s*[!?.,]*$"))
async def marry_cmd(message: Message) -> None:
    if not message.from_user or not is_group(message):
        await message.answer("💍 Брак можно предложить в группе, ответив на сообщение участника командой /marry.")
        return
    if not message.reply_to_message or not message.reply_to_message.from_user:
        await message.answer("Ответьте на сообщение человека командой «брак».")
        return
    partner = message.reply_to_message.from_user
    if partner.id == message.from_user.id or partner.is_bot:
        await message.answer("С этим пользователем нельзя заключить брак.")
        return
    rows = await asyncio.to_thread(db_rows, "bot_marriages", 1000, None, chat_id=message.chat.id)
    for row in rows:
        if row.get("status") not in {"married", "pending"}:
            continue
        ids = {int(row.get("user1_id") or 0), int(row.get("user2_id") or 0)}
        if message.from_user.id in ids or partner.id in ids:
            if row.get("status") == "married":
                row_one = int(row.get("user1_id") or 0)
                row_two = int(row.get("user2_id") or 0)
                if message.from_user.id in (row_one, row_two):
                    spouse_id = row_two if message.from_user.id == row_one else row_one
                    spouse_name = str(row.get("user2_name") or "пользователь") if spouse_id == row_two else str(row.get("user1_name") or "пользователь")
                    await message.answer(f"📝 Вы уже состоите в браке с {mention(spouse_id, spouse_name)}. Сначала разведитесь командой «развод».")
                else:
                    await message.answer(f"📝 К сожалению, {escape(partner.full_name)} уже состоит в браке. Сначала ему нужно развестись.")
                return
            if int(row.get("user1_id") or 0) == message.from_user.id and int(row.get("user2_id") or 0) == partner.id:
                await message.answer("💌 Предложение этому пользователю уже отправлено — дождитесь ответа.")
                return
            await message.answer("💍 У одного из вас уже есть ожидающее предложение брака. Сначала примите или отклоните его.")
            return
    inserted = await asyncio.to_thread(db_insert, "bot_marriages", {
        "chat_id": message.chat.id, "user1_id": message.from_user.id,
        "user1_name": message.from_user.full_name, "user2_id": partner.id,
        "user2_name": partner.full_name, "status": "pending",
    })
    if not inserted or not inserted.get("id"):
        await message.answer("Не удалось создать предложение. Проверь подключение Supabase и таблицу bot_marriages.")
        return
    marriage_id = int(inserted["id"])
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💍 Принять брак", callback_data=f"marriage:accept:{marriage_id}"),
         InlineKeyboardButton(text="💔 Отклонить брак", callback_data=f"marriage:decline:{marriage_id}")]
    ])
    await message.answer(
        f"💍 {mention(partner.id, partner.full_name)}, вам пришло предложение о браке!\n\n"
        f"💖 {mention(message.from_user.id, message.from_user.full_name)} предлагает вам стать супругами.\n"
        "Нажмите одну из кнопок ниже, чтобы принять или отклонить предложение.",
        reply_markup=keyboard,
    )

@router.message(Command("cancelmarry", "cancelmarriage"))
async def cancel_marriage_proposal_cmd(message: Message) -> None:
    """Cancel the caller's own pending proposal in this chat."""
    if not message.from_user or not is_group(message):
        await message.answer("💍 Отменить своё предложение можно в группе.")
        return
    if db is None:
        await message.answer("Не удалось проверить предложение: база данных не настроена.")
        return
    try:
        result = await asyncio.to_thread(
            lambda: db.table("bot_marriages").update({"status": "cancelled"})
            .eq("chat_id", message.chat.id)
            .eq("user1_id", message.from_user.id)
            .eq("status", "pending")
            .select("*").execute()
        )
        cancelled = result.data or []
    except Exception:
        log.exception("Could not cancel pending marriage proposal chat=%s user=%s",
                      message.chat.id, message.from_user.id)
        await message.answer("Не удалось отменить предложение из-за ошибки базы данных. Попробуйте позже.")
        return
    if not cancelled:
        await message.answer("У вас нет ожидающего предложения брака, которое можно отменить.")
        return
    await message.answer("💔 Ваше предложение брака отменено. Старые кнопки больше не сработают.")


@router.callback_query(F.data.startswith("marriage:"))
async def marriage_proposal_callback(callback: CallbackQuery) -> None:
    if not callback.from_user or not callback.data:
        await callback.answer("Не удалось определить пользователя.", show_alert=True)
        return
    if callback.from_user.id != OWNER_ID and await asyncio.to_thread(_is_globally_banned, callback.from_user.id):
        await callback.answer("Доступ к функциям Pear CM ограничен.", show_alert=True)
        return
    parts = callback.data.split(":")
    if len(parts) != 3 or parts[1] not in {"accept", "decline"}:
        await callback.answer("Некорректное действие.", show_alert=True)
        return
    try:
        marriage_id = int(parts[2])
    except ValueError:
        await callback.answer("Некорректное предложение.", show_alert=True)
        return
    row = await asyncio.to_thread(db_one, "bot_marriages", id=marriage_id)
    if not row or row.get("status") != "pending":
        await callback.answer("Это предложение уже неактивно.", show_alert=True)
        return
    if int(row.get("chat_id") or 0) != (callback.message.chat.id if callback.message else int(row.get("chat_id") or 0)):
        await callback.answer("Предложение из другого чата.", show_alert=True)
        return
    if int(row.get("user2_id") or 0) != callback.from_user.id:
        await callback.answer("Эти кнопки предназначены только тому, кому предложили брак.", show_alert=True)
        return
    new_status = "married" if parts[1] == "accept" else "divorced"
    try:
        result = await asyncio.to_thread(
            lambda: db.table("bot_marriages").update({
                "status": new_status,
                "accepted_at": datetime.now(timezone.utc).isoformat() if new_status == "married" else None,
            }).eq("id", marriage_id).eq("status", "pending").select("*").execute()
        )
        if not result.data:
            await callback.answer("Предложение уже обработано.", show_alert=True)
            return
    except Exception:
        log.exception("Could not process marriage proposal callback")
        await callback.answer("Не удалось сохранить ответ. Попробуйте ещё раз.", show_alert=True)
        return
    proposer_id = int(row.get("user1_id") or 0)
    proposer_name = str(row.get("user1_name") or "Участник")
    partner_name = str(row.get("user2_name") or callback.from_user.full_name)
    if parts[1] == "accept":
        text = f"💍 {mention(proposer_id, proposer_name)} и {mention(callback.from_user.id, partner_name)} теперь состоят в браке! Поздравляем!"
        await callback.answer("Брак принят! 💍")
    else:
        text = f"💔 {mention(callback.from_user.id, partner_name)} отклонил(а) предложение о браке от {mention(proposer_id, proposer_name)}."
        await callback.answer("Предложение отклонено.")
    if callback.message:
        try:
            await callback.message.edit_text(text)
        except TelegramBadRequest:
            await callback.message.answer(text)

@router.message(Command("acceptmarry"))
@router.message(F.text.regexp(r"(?i)^\s*[.!/]?(?:брак да|принять брак|согласен на брак|согласна на брак|да согласен|да согласна|принимаю предложение)(?:\s|$)"))
async def accept_marry_cmd(message: Message) -> None:
    if not message.from_user:
        return
    pending = await asyncio.to_thread(db_rows, "bot_marriages", 1000, "created_at", chat_id=message.chat.id, user2_id=message.from_user.id, status="pending")
    if not pending:
        await message.answer("Активное предложение вам не найдено.")
        return
    row = pending[0]
    if message.reply_to_message and message.reply_to_message.from_user and not message.reply_to_message.from_user.is_bot:
        if int(row.get("user1_id") or 0) != message.reply_to_message.from_user.id:
            await message.answer("Ответьте на сообщение того, кто предложил вам брак, или используйте кнопку в предложении.")
            return
    await _finish_marriage_by_command(message, row, True)

@router.message(Command("declinemarry"))
@router.message(F.text.regexp(r"(?i)^\s*[.!/]?(?:брак нет|отклонить брак|не согласен на брак|не согласна на брак)(?:\s|$)"))
async def decline_marry_cmd(message: Message) -> None:
    if not message.from_user:
        return
    pending = await asyncio.to_thread(db_rows, "bot_marriages", 1000, "created_at", chat_id=message.chat.id, user2_id=message.from_user.id, status="pending")
    if not pending:
        await message.answer("Активное предложение вам не найдено.")
        return
    await _finish_marriage_by_command(message, pending[0], False)

async def _finish_marriage_by_command(message: Message, row: dict, accept: bool) -> None:
    try:
        status = "married" if accept else "divorced"
        result = await asyncio.to_thread(lambda: db.table("bot_marriages").update({
            "status": status,
            "accepted_at": datetime.now(timezone.utc).isoformat() if accept else None,
        }).eq("id", row["id"]).eq("status", "pending").select("*").execute())
        if not result.data:
            await message.answer("Это предложение уже обработано.")
            return
    except Exception:
        log.exception("Could not finish marriage from command")
        await message.answer("Не удалось сохранить ответ в базе данных.")
        return
    proposer_id = int(row.get("user1_id") or 0)
    proposer_name = str(row.get("user1_name") or "Участник")
    partner_name = str(row.get("user2_name") or message.from_user.full_name)
    if accept:
        await message.answer(f"💍 {mention(proposer_id, proposer_name)} и {mention(message.from_user.id, partner_name)} теперь состоят в браке! Поздравляем!")
    else:
        await message.answer(f"💔 {mention(message.from_user.id, partner_name)} отклонил(а) предложение о браке от {mention(proposer_id, proposer_name)}.")
@router.message(Command("marriage", "mymarriage", "myrelations", "relationships"))
@router.message(F.text.regexp(r"(?i)^\s*[.!/]?(?:мой брак|моя свадьба|мои отношения|мои отн|мои отнош|мои браки|брак мой|отношения|отн)(?:\s|$)"))
async def marriage_status_cmd(message: Message) -> None:
    if not message.from_user:
        return
    if _feature_normalize(message.text or "") in {"отн действия", "отношения действия", "действия отношений", "список действий"}:
        await message.answer("💞 <b>Действия отношений</b>\n• обнять\n• поцеловать\n• поддержать\n• погладить\n• подарить айфон\n• подарить цветы\n• подарить шоколад\n• подарить кольцо\n• подарить машину\n• пожать руку\n• дать пять\n• поздравить\n• утешить\n• успокоить\n• покормить\n• угостить\n• пригласить на свидание\n• помочь\n• спасти\n• простить\n• помириться\n\nОтветь на сообщение участника нужной командой.")
        return
    rows = await asyncio.to_thread(db_rows, "bot_marriages", limit=1000, chat_id=message.chat.id)
    active_marriage = next((row for row in rows if row.get("status") == "married" and
        message.from_user.id in (int(row.get("user1_id") or 0), int(row.get("user2_id") or 0))), None)
    if active_marriage:
        one_id, two_id = int(active_marriage.get("user1_id") or 0), int(active_marriage.get("user2_id") or 0)
        one_name, two_name = str(active_marriage.get("user1_name") or "Участник"), str(active_marriage.get("user2_name") or "Участник")
        start_at = active_marriage.get("accepted_at") or active_marriage.get("created_at")
        duration = ""
        try:
            start_dt = datetime.fromisoformat(str(start_at).replace("Z", "+00:00"))
            days = max(0, (datetime.now(timezone.utc) - start_dt.astimezone(timezone.utc)).days)
            duration = f"\n⏱ Длится уже {days // 30} мес. {days % 30} дн."
        except Exception:
            pass
        response = (f"👰💍 Брак между {mention(one_id, one_name)} и {mention(two_id, two_name)}"
                    f"\n🗓 Зарегистрирован {str(start_at or 'дата неизвестна')[:10]}{duration}")
        raw_rel = (message.text or "").lower()
        if "мои отн" in raw_rel or "отнош" in raw_rel or raw_rel.strip().endswith("отн"):
            try:
                links = await asyncio.to_thread(db_rows, "bot_relationships", 1000, "points", chat_id=message.chat.id)
                links = [r for r in links if message.from_user.id in (int(r.get("user1_id") or 0), int(r.get("user2_id") or 0))]
                links.sort(key=lambda r: int(r.get("points") or 0), reverse=True)
                if links:
                    users = await asyncio.to_thread(db_rows, "bot_users", 1000, "messages", chat_id=message.chat.id)
                    names = {int(u.get("user_id") or 0): str(u.get("first_name") or "Участник") for u in users}
                    response += "\n\n💞 <b>Другие отношения</b>"
                    for rel in links[:10]:
                        aa, bb = int(rel.get("user1_id") or 0), int(rel.get("user2_id") or 0)
                        other = bb if aa == message.from_user.id else aa
                        response += f"\n• {mention(other, names.get(other, 'Участник'))} — {int(rel.get('points') or 0)} 💗"
            except Exception:
                log.exception("Could not fetch roleplay relationships")
        await message.answer(response)
        return
    pending = [
        r for r in rows
        if r.get("status") == "pending"
        and message.from_user.id in (int(r.get("user1_id") or 0), int(r.get("user2_id") or 0))
    ]
    if pending:
        lines = ["💌 <b>Ваши предложения брака</b>"]
        for row in pending[:10]:
            proposer_name = escape(str(row.get("user1_name") or "Участник"))
            partner_name = escape(str(row.get("user2_name") or "Участник"))
            lines.append(f"• {proposer_name} ❤️ {partner_name}")
        await message.answer("\n".join(lines))
        return
    raw_status = (message.text or "").lower()
    if "отн" in raw_status or "отнош" in raw_status or "роман" in raw_status:
        try:
            links = await asyncio.to_thread(db_rows, "bot_relationships", 1000, "points", chat_id=message.chat.id)
            links = [r for r in links if message.from_user.id in (int(r.get("user1_id") or 0), int(r.get("user2_id") or 0))]
            links.sort(key=lambda r: int(r.get("points") or 0), reverse=True)
            users = await asyncio.to_thread(db_rows, "bot_users", 1000, "messages", chat_id=message.chat.id)
            names = {int(u.get("user_id") or 0): str(u.get("first_name") or "Участник") for u in users}
            lines = ["💞 <b>Ваши отношения</b>", "💔 Активного брака пока нет."]
            if links:
                lines.append("✨ <b>Связи в чате</b>")
                for rel in links[:15]:
                    one, two = int(rel.get("user1_id") or 0), int(rel.get("user2_id") or 0)
                    other = two if one == message.from_user.id else one
                    lines.append(f"• {mention(other, names.get(other, 'Участник'))} — {int(rel.get('points') or 0)} 💗")
            else:
                lines.append("Пока нет накопленных очков отношений. Используй команды вроде «обнять», «поцеловать» или «поддержать» в ответ на сообщение.")
            await message.answer("\n".join(lines))
            return
        except Exception:
            log.exception("Could not fetch relationships without marriage")
    await message.answer("💔 Вы пока не состоите в браке в этой группе. Чтобы предложить брак, ответьте на сообщение участника командой «брак» или «пожениться».")

@router.message(Command("divorce", "breakup"))
@router.message(F.text.regexp(r"(?i)^\s*[.!/]?(?:развод|развестись|разведись|разрываем брак|расторгнуть брак|расстаться)(?:\s|$)"))
@router.message(F.text.regexp(r"(?i)^[.!/]?(?:развод|развестись)(?:\s|$)"))
async def divorce_cmd(message: Message) -> None:
    if not message.from_user:
        return
    rows = await asyncio.to_thread(db_rows, "bot_marriages", limit=1000, chat_id=message.chat.id)
    for row in rows:
        if row.get("status") == "married" and message.from_user.id in (row.get("user1_id"), row.get("user2_id")):
            try:
                result = await asyncio.to_thread(
                    lambda: db.table("bot_marriages").update({"status": "divorced"})
                    .eq("id", row["id"]).eq("status", "married").select("*").execute()
                )
                if not result.data:
                    await message.answer("ℹ️ Брак уже изменён другим действием. Обнови статус командой «мой брак».")
                    return
                await message.answer("💔 Брак расторгнут.")
            except Exception:
                log.exception("Could not divorce marriage id=%s chat=%s", row.get("id"), message.chat.id)
                await message.answer("Не удалось изменить запись в базе данных. Брак не подтверждён как расторгнутый.")
            return
    await message.answer("У вас нет активного брака.")

# ── Integrated moderation stages from moderation.py ──────────────────────────
# Stage A: durable per-chat filter settings and human-readable time/reason parsing.
def get_chat_settings(chat_id: int, force_refresh: bool = False) -> dict[str, Any]:
    now = time.monotonic()
    cached = _CHAT_SETTINGS_CACHE.get(chat_id)
    cached_at = _CHAT_SETTINGS_CACHE_TIME.get(chat_id, 0.0)
    if not force_refresh and cached is not None and now - cached_at < _CHAT_SETTINGS_CACHE_TTL:
        return dict(cached)
    settings = dict(FILTER_DEFAULTS)
    row = db_one("bot_chat_settings", chat_id=chat_id)
    if row and isinstance(row.get("settings"), dict):
        settings.update(row["settings"])
    _CHAT_SETTINGS_CACHE[chat_id] = dict(settings)
    _CHAT_SETTINGS_CACHE_TIME[chat_id] = now
    if len(_CHAT_SETTINGS_CACHE) > 2000:
        cutoff = now - _CHAT_SETTINGS_CACHE_TTL * 4
        for cached_chat, stamp in list(_CHAT_SETTINGS_CACHE_TIME.items()):
            if stamp < cutoff:
                _CHAT_SETTINGS_CACHE.pop(cached_chat, None)
                _CHAT_SETTINGS_CACHE_TIME.pop(cached_chat, None)
    return dict(settings)

def save_chat_settings(chat_id: int, settings: dict[str, Any]) -> None:
    # Update the in-memory state immediately; persist in a worker thread so a
    # slow Supabase response cannot stall commands or other chat updates.
    snapshot = dict(settings)
    _CHAT_SETTINGS_CACHE[chat_id] = snapshot
    _CHAT_SETTINGS_CACHE_TIME[chat_id] = time.monotonic()
    payload = {
        "chat_id": chat_id, "settings": snapshot,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    async def persist() -> None:
        try:
            saved = await asyncio.to_thread(db_upsert, "bot_chat_settings", payload, "chat_id")
            if not saved:
                log.error("Could not persist chat settings chat=%s keys=%s", chat_id, sorted(snapshot.keys()))
        except Exception:
            log.exception("Could not persist chat settings chat=%s", chat_id)
    try:
        asyncio.get_running_loop().create_task(persist())
    except RuntimeError:
        saved = db_upsert("bot_chat_settings", payload, "chat_id")
        if not saved:
            log.error("Could not persist chat settings outside event loop chat=%s", chat_id)

FILTER_ALIASES = {
    "антиссылка": "antilink", "антилинк": "antilink",
    "антиспам": "antispam", "антиинвайт": "antiinvite", "антивызов": "antiinvite",
    "антимат": "antimat", "антифлуд": "antiflood", "антикапс": "anticaps",
    "антиреклама": "antireklama", "антикрипта": "antikripta",
    "антифишинг": "antifishing", "антиолл": "antiall", "антиалл": "antiall",
    "антибот": "antibot", "антистикер": "antisticker", "антигиф": "antigif",
    "антифото": "antiphoto", "антивидео": "antivideo", "антиголос": "antivoice",
    "антидок": "antidocument", "антидокумент": "antidocument",
}

@router.message(F.text.regexp(r"(?i)^[+-](?:антиссылка|антилинк|антиспам|антиинвайт|антивызов|антимат|антифлуд|антикапс|антиреклама|антикрипта|антифишинг|антиолл|антиалл|антибот|антистикер|антигиф|антифото|антивидео|антиголос|антидок|антидокумент)(?:\s|$)"))
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
    settings = await asyncio.to_thread(get_chat_settings, message.chat.id)
    settings[key] = enabled
    save_chat_settings(message.chat.id, settings)
    await message.answer(f"{'✅ Включён' if enabled else '🛑 Выключен'} фильтр <b>{alias}</b>.")

@router.message(F.text.regexp(r"(?i)^[.!/]?(?:ранг|rank)(?:\s|$)"))
async def rank_cmd(message: Message) -> None:
    parts = (message.text or "").split()
    if len(parts) == 1:
        if not message.from_user:
            return
        current_rank = await asyncio.to_thread(user_rank, message.chat.id, message.from_user.id)
        await message.answer(f"🏅 Ваш ранг: <b>{current_rank}</b> — <b>{role_name(current_rank)}</b>.")
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
    if message.from_user and target[0] == message.from_user.id:
        await message.answer("⛔ Нельзя менять собственный ранг.")
        return
    actor_member = await get_chat_member_cached(bot, message.chat.id, message.from_user.id)
    if actor_member is None and message.from_user.id != OWNER_ID:
        await message.answer("⛔ Не удалось проверить ваши права в Telegram.")
        return
    is_chat_creator = bool(actor_member and actor_member.status == ChatMemberStatus.CREATOR) or message.from_user.id == OWNER_ID
    target_existing, actor_existing = await asyncio.gather(
        asyncio.to_thread(db_one, "bot_users", user_id=target[0], chat_id=message.chat.id),
        asyncio.to_thread(db_one, "bot_users", user_id=message.from_user.id, chat_id=message.chat.id),
    )
    target_existing = target_existing or {}
    actor_existing = actor_existing or {}
    try:
        target_existing_rank = max(0, min(5, int(target_existing.get("rank", 0) or 0)))
    except (TypeError, ValueError):
        target_existing_rank = 0
    try:
        actor_existing_rank = max(0, min(5, int(actor_existing.get("rank", 0) or 0)))
    except (TypeError, ValueError):
        actor_existing_rank = 0
    effective_actor_rank = 5 if is_chat_creator else max(4 if message.from_user.id == OWNER_ID else 3, actor_existing_rank)
    if target[0] == OWNER_ID and message.from_user.id != OWNER_ID:
        await message.answer("⛔ Ранг владельца Pear CM защищён от изменений.")
        return
    if target_existing_rank >= 5 and not is_chat_creator:
        await message.answer("⛔ Ранг 5 защищён: изменить его может только настоящий создатель Telegram-чата.")
        return
    if not is_chat_creator and rank >= effective_actor_rank:
        await message.answer("⛔ Вы можете назначать только ранг ниже своего. Ранг 5 может назначать только создатель чата или владелец Pear CM.")
        return
    if not is_chat_creator and target_existing_rank >= effective_actor_rank and target_existing_rank > 0:
        await message.answer("⛔ Нельзя менять ранг равного или старшего участника.")
        return
    if message.from_user.id == OWNER_ID and rank > 4:
        await message.answer("⛔ Вы — старший администратор (ранг 4). Ранг 5 принадлежит создателю Telegram-чата и не назначается через Pear CM.")
        return
    user = target_existing
    saved = await asyncio.to_thread(db_upsert, "bot_users", {**user, "user_id": target[0], "chat_id": message.chat.id,
        "username": user.get("username"), "first_name": user.get("first_name") or target[1],
        "warns": int(user.get("warns", 0)), "messages": int(user.get("messages", 0)), "rank": rank}, "user_id,chat_id")
    if not saved:
        await message.answer("⚠️ Не удалось сохранить ранг. Проверь подключение Supabase и таблицу bot_users.")
        return
    invalidate_user_rank(message.chat.id, target[0])
    audit(message.chat.id, message.from_user.id, "set_rank", target[0], f"rank={rank}")
    await message.answer(f"🏅 Ранг {mention(target[0], target[1])} установлен: <b>{rank} — {role_name(rank)}</b>.")


@router.message(F.text.regexp(r"(?i)^[.!/]?(?:повысить(?:\s*[- ]?\s*ранг)?|повышени(?:е|я|ю|ем|и)|повышай|повышайся|продвинуть|поднять(?:\s+ранг)?|назначить\s+(?:ранг|чин)|присвоить\s+ранг|promote)(?:\s|$)"))
async def promote_rank_cmd(message: Message) -> None:
    if not await require_real_admin(message):
        return
    remainder = re.sub(r"(?i)^[.!/]?(?:повысить(?:\s*[- ]?\s*ранг)?|повышени(?:е|я|ю|ем|и)|повышай|повышайся|продвинуть|поднять(?:\s+ранг)?|назначить\s+(?:ранг|чин)|присвоить\s+ранг|promote)\s*", "", (message.text or "").strip(), count=1).strip()
    tokens = remainder.split()
    explicit = None
    for token in tokens:
        if token.isdigit():
            if not 0 <= int(token) <= 5:
                await message.answer("⛔ Ранг должен быть числом от 0 до 5.")
                return
            explicit = int(token)
            break
    target_args = [token for token in tokens if not (token.isdigit() and explicit is not None and int(token) == explicit)]
    target = await resolve_target(message, target_args)
    if not target:
        await message.answer("Ответьте на сообщение участника: <code>повысить</code> — на один ранг; <code>повысить 4</code> — назначить 4-й ранг.")
        return
    if message.from_user and target[0] == message.from_user.id:
        await message.answer("⛔ Нельзя менять собственный ранг.")
        return
    user, actor_rank_loaded = await asyncio.gather(
        asyncio.to_thread(db_one, "bot_users", user_id=target[0], chat_id=message.chat.id),
        asyncio.to_thread(user_rank, message.chat.id, message.from_user.id if message.from_user else 0),
    )
    user = user or {}
    try:
        current = 5 if target[0] == OWNER_ID else max(0, min(5, int(user.get("rank", 0) or 0)))
    except (TypeError, ValueError):
        current = 0
    new_rank = explicit if explicit is not None else current + 1
    if new_rank <= current:
        await message.answer(f"ℹ️ У участника уже ранг {current} — {role_name(current)}. Для повышения выберите ранг выше.")
        return
    if new_rank > 5:
        await message.answer("ℹ️ Достигнут максимальный ранг: Создатель.")
        return
    actor_is_owner = bool(message.from_user and message.from_user.id == OWNER_ID)
    actor_member = await get_chat_member_cached(bot, message.chat.id, message.from_user.id) if message.from_user else None
    actor_is_creator = bool(actor_member and actor_member.status == ChatMemberStatus.CREATOR)
    target_member = await get_chat_member_cached(bot, message.chat.id, target[0])
    if target_member and target_member.status == ChatMemberStatus.CREATOR:
        await message.answer("⛔ Нельзя изменять ранг создателя Telegram-чата.")
        return
    if new_rank == 5 and not (actor_is_owner or actor_is_creator):
        await message.answer("⛔ Ранг 5 «Создатель» может назначить только создатель чата или владелец Pear CM.")
        return
    actor_is_telegram_admin = bool(actor_member and actor_member.status == ChatMemberStatus.ADMINISTRATOR)
    actor_rank = 5 if (actor_is_owner or actor_is_creator) else max(int(actor_rank_loaded or 0), 4 if actor_is_telegram_admin else 0)
    if not (actor_is_owner or actor_is_creator) and (current >= actor_rank or new_rank >= actor_rank):
        await message.answer("⛔ Можно менять только ранг участника ниже своего и назначать ранг строго ниже своего.")
        return
    recent = _RECENT_CHAT_USERS.get((message.chat.id, target[0]))
    recent_username = recent[0] if recent else None
    recent_first_name = recent[1] if recent else None
    fallback_username = target[1][1:] if target[1].startswith("@") else None
    fallback_first_name = target[1][1:] if target[1].startswith("@") else target[1]
    payload = {**user, "user_id": target[0], "chat_id": message.chat.id,
        "username": user.get("username") or recent_username or fallback_username,
        "first_name": user.get("first_name") or recent_first_name or fallback_first_name,
        "warns": int(user.get("warns", 0)), "messages": int(user.get("messages", 0)), "rank": new_rank}
    saved = await asyncio.to_thread(db_upsert, "bot_users", payload, "user_id,chat_id")
    if not saved:
        await message.answer("⚠️ Не удалось сохранить повышение ранга. Проверь подключение Supabase и таблицу bot_users.")
        return
    invalidate_user_rank(message.chat.id, target[0])
    audit(message.chat.id, message.from_user.id if message.from_user else 0, "promote", target[0], f"rank={current}->{new_rank}")
    await message.answer(f"⬆️ <b>{escape(target[1])}</b> был повышен до роли <b>{role_name(new_rank)}</b>.")


@router.message(F.text.regexp(r"(?i)^[.!/]?(?:понизить\s+в\s+должности|снять\s+(?:ранг|чин|админ(?:а|у|ом|е)?|администратор(?:а|у|ом|е)?|модератор(?:а|у|ом|е)?)|убрать\s+(?:ранг|чин)|лишить\s+ранга|понизить(?:\s*[- ]?\s*ранг)?|понижени(?:е|я|ю|ем|и)|понижай|понизь|разжаловать|demote)(?:\s|$)"))
async def demote_rank_cmd(message: Message) -> None:
    if not await require_real_admin(message):
        return
    remainder = re.sub(r"(?i)^[.!/]?(?:понизить\s+в\s+должности|снять\s+(?:ранг|чин|админ(?:а|у|ом|е)?|администратор(?:а|у|ом|е)?|модератор(?:а|у|ом|е)?)|убрать\s+(?:ранг|чин)|лишить\s+ранга|понизить(?:\s*[- ]?\s*ранг)?|понижени(?:е|я|ю|ем|и)|понижай|понизь|разжаловать|demote)\s*", "", (message.text or "").strip(), count=1).strip()
    tokens = remainder.split()
    explicit = None
    for token in tokens:
        if token.isdigit():
            if not 0 <= int(token) <= 5:
                await message.answer("⛔ Ранг должен быть числом от 0 до 5.")
                return
            explicit = int(token)
            break
    target_args = [token for token in tokens if not (token.isdigit() and explicit is not None and int(token) == explicit)]
    target = await resolve_target(message, target_args)
    if not target:
        await message.answer("Ответьте на сообщение участника: <code>понизить</code> — на один ранг; <code>понизить 3</code> — назначить 3-й ранг.")
        return
    if message.from_user and target[0] == message.from_user.id:
        await message.answer("⛔ Нельзя менять собственный ранг.")
        return
    user, actor_rank_loaded = await asyncio.gather(
        asyncio.to_thread(db_one, "bot_users", user_id=target[0], chat_id=message.chat.id),
        asyncio.to_thread(user_rank, message.chat.id, message.from_user.id if message.from_user else 0),
    )
    user = user or {}
    try:
        current = 5 if target[0] == OWNER_ID else max(0, min(5, int(user.get("rank", 0) or 0)))
    except (TypeError, ValueError):
        current = 0
    new_rank = explicit if explicit is not None else current - 1
    if new_rank >= current:
        await message.answer(f"ℹ️ У участника уже ранг {current} — {role_name(current)}. Для понижения выберите ранг ниже.")
        return
    if new_rank < 0:
        await message.answer("ℹ️ Уже минимальный ранг: Участник.")
        return
    actor_is_owner = bool(message.from_user and message.from_user.id == OWNER_ID)
    actor_member = await get_chat_member_cached(bot, message.chat.id, message.from_user.id) if message.from_user else None
    actor_is_creator = bool(actor_member and actor_member.status == ChatMemberStatus.CREATOR)
    target_member = await get_chat_member_cached(bot, message.chat.id, target[0])
    if target_member and target_member.status == ChatMemberStatus.CREATOR:
        await message.answer("⛔ Нельзя изменять ранг создателя Telegram-чата.")
        return
    if current == 5 and not (actor_is_owner or actor_is_creator):
        await message.answer("⛔ Понижать ранг 5 «Создатель» может только создатель чата или владелец Pear CM.")
        return
    actor_is_telegram_admin = bool(actor_member and actor_member.status == ChatMemberStatus.ADMINISTRATOR)
    actor_rank = 5 if (actor_is_owner or actor_is_creator) else max(int(actor_rank_loaded or 0), 4 if actor_is_telegram_admin else 0)
    if not (actor_is_owner or actor_is_creator) and (current >= actor_rank or new_rank >= actor_rank):
        await message.answer("⛔ Можно менять только ранг участника ниже своего и назначать ранг строго ниже своего.")
        return
    recent = _RECENT_CHAT_USERS.get((message.chat.id, target[0]))
    recent_username = recent[0] if recent else None
    recent_first_name = recent[1] if recent else None
    fallback_username = target[1][1:] if target[1].startswith("@") else None
    fallback_first_name = target[1][1:] if target[1].startswith("@") else target[1]
    payload = {**user, "user_id": target[0], "chat_id": message.chat.id,
        "username": user.get("username") or recent_username or fallback_username,
        "first_name": user.get("first_name") or recent_first_name or fallback_first_name,
        "warns": int(user.get("warns", 0)), "messages": int(user.get("messages", 0)), "rank": new_rank}
    saved = await asyncio.to_thread(db_upsert, "bot_users", payload, "user_id,chat_id")
    if not saved:
        await message.answer("⚠️ Не удалось сохранить понижение ранга. Проверь подключение Supabase и таблицу bot_users.")
        return
    invalidate_user_rank(message.chat.id, target[0])
    audit(message.chat.id, message.from_user.id if message.from_user else 0, "demote", target[0], f"rank={current}->{new_rank}")
    await message.answer(f"⬇️ <b>{escape(target[1])}</b> был понижен до роли <b>{role_name(new_rank)}</b>.")


@router.message(F.text.regexp(r"(?i)^\.дк(?:\s|$)"))
async def command_rank_rule_cmd(message: Message) -> None:
    if not await require_real_admin(message):
        return
    raw = (message.text or "").strip()
    parts = raw.split()
    if len(parts) == 1:
        settings = await asyncio.to_thread(get_chat_settings, message.chat.id)
        default_rank = int(settings.get("modrank_default", 1) or 1)
        rules = []
        for key, label in (("mute","мут"),("unmute","размут"),("ban","бан"),("unban","разбан"),
                           ("kick","кик"),("warn","варн"),("unwarn","снять варн"),("promote","повысить"),
                           ("demote","понизить"),("purge","очистить"),("delete","удалить сообщения"),
                           ("pin","закрепить"),("unpin","открепить")):
            rules.append(f"• {label}: ранг {settings.get('modrank_'+key, default_rank)}")
        await message.answer(
            "<b>🔐 Доступ к командам (.дк)</b>\n"
            f"Общий порог: <b>{default_rank}</b> — {role_name(default_rank)}\n" +
            "\n".join(rules) +
            "\n\nПримеры: <code>.дк мут 2</code>, <code>.дк мутами 3</code>, "
            "<code>.дк снять все варны 4</code>, <code>.дк общий 2</code>."
        )
        return
    if len(parts) == 2 and parts[1].isdigit():
        required = int(parts[1])
        if not 1 <= required <= 5:
            await message.answer("Ранг должен быть от 1 до 5.")
            return
        settings = await asyncio.to_thread(get_chat_settings, message.chat.id)
        settings["modrank_default"] = required
        save_chat_settings(message.chat.id, settings)
        await message.answer(
            f"🔐 Общий порог доступа установлен: <b>{required} — {role_name(required)}</b>. "
            "Индивидуальные правила команд имеют приоритет."
        )
        return
    if len(parts) < 3:
        await message.answer("Формат: <code>.дк 3</code> или <code>.дк мутами 2</code>. Ранг — от 1 до 5.")
        return
    try:
        required = int(parts[-1])
    except ValueError:
        required = 0
    if not 1 <= required <= 5:
        await message.answer("Последним аргументом укажи ранг от 1 до 5.")
        return
    command_phrase = " ".join(parts[1:-1]).lower().replace("ё", "е")
    command_phrase = re.sub(r"[\W_]+", " ", command_phrase, flags=re.UNICODE).strip()
    exact_aliases = {
        "мут":"mute", "замут":"mute", "замутить":"mute", "замуть":"mute", "дать мут":"mute", "выдать мут":"mute",
        "размут":"unmute", "анмут":"unmute", "унмут":"unmute", "снять мут":"unmute", "снять муту":"unmute",
        "убрать мут":"unmute", "убрать муту":"unmute", "снятьмут":"unmute",
        "бан":"ban", "забанить":"ban", "дать бан":"ban", "выдать бан":"ban",
        "разбан":"unban", "снять бан":"unban", "разбанить":"unban",
        "кик":"kick", "кикнуть":"kick", "выгнать":"kick", "исключить":"kick",
        "варн":"warn", "варны":"warn", "варнами":"warn", "варнов":"warn", "предупреждение":"warn",
        "предупреждения":"warn", "предупредить":"warn", "дать варн":"warn", "выдать варн":"warn",
        "снять варн":"unwarn", "снять варны":"unwarn", "снять все варны":"unwarn", "убрать варн":"unwarn",
        "убрать варны":"unwarn", "удалить варн":"unwarn", "удалить варны":"unwarn",
        "снять предупреждение":"unwarn", "снять предупреждения":"unwarn", "убрать предупреждение":"unwarn",
        "анварн":"unwarn", "повысить":"promote", "повышать":"promote", "повышение":"promote",
        "повысить ранг":"promote", "повысить рангом":"promote", "повыситьранг":"promote",
        "повысить ранг участника":"promote", "повысить пользователя":"promote", "повышай":"promote",
        "продвинуть":"promote", "назначить ранг":"promote",
        "понизить":"demote", "понижать":"demote", "понижение":"demote", "понизить ранг":"demote",
        "понизитьранг":"demote", "понизить ранг участника":"demote", "понизить пользователя":"demote",
        "понижай":"demote", "понизь":"demote",
        "очистить":"purge", "очистка":"purge", "удалить последние сообщения":"purge",
        "смс":"delete", "сообщение":"delete", "сообщения":"delete", "удалить сообщения":"delete",
        "пин":"pin", "пинить":"pin", "закрепить":"pin", "закрепление":"pin",
        "анпин":"unpin", "открепить":"unpin", "снять закреп":"unpin", "открепление":"unpin",
        "общий":"__default__", "общий доступ":"__default__", "все команды":"__default__",
    }
    key = exact_aliases.get(command_phrase)
    if key is None:
        tokens = command_phrase.split()
        # Resolve grammatical forms by stable stems, while preferring the longest intent.
        if any(t.startswith(("размут", "анмут", "унмут", "снят", "снять", "убрат", "убрать", "отключ")) for t in tokens) and any(t.startswith("мут") for t in tokens):
            key = "unmute"
        elif any(t.startswith(("мут", "замут")) for t in tokens):
            key = "mute"
        elif any(t.startswith(("разбан", "снят", "снять", "убрат", "убрать", "отмен")) for t in tokens) and any(t.startswith("бан") for t in tokens):
            key = "unban"
        elif any(t.startswith(("бан", "забан", "выдан")) for t in tokens):
            key = "ban"
        elif any(t.startswith(("кик", "выгна", "исключ")) for t in tokens):
            key = "kick"
        elif any(t.startswith(("снят", "убрат", "удал", "анварн")) for t in tokens) and any(t.startswith(("варн", "предупрежд")) for t in tokens):
            key = "unwarn"
        elif any(t.startswith(("варн", "предупрежд")) for t in tokens):
            key = "warn"
        elif any(t.startswith(("повыс", "повыш", "продвин", "назнач")) for t in tokens):
            key = "promote"
        elif any(t.startswith(("пониз", "пониж")) for t in tokens):
            key = "demote"
        elif any(t.startswith(("очист", "удален")) for t in tokens):
            key = "purge"
        elif any(t.startswith(("смс", "сообщен", "удал")) for t in tokens):
            key = "delete"
        elif any(t.startswith(("анпин", "откреп", "снят")) for t in tokens) and any(t.startswith(("пин", "закреп")) for t in tokens):
            key = "unpin"
        elif any(t.startswith(("пин", "закреп")) for t in tokens):
            key = "pin"
    if key == "__default__":
        settings = await asyncio.to_thread(get_chat_settings, message.chat.id)
        settings["modrank_default"] = required
        save_chat_settings(message.chat.id, settings)
        await message.answer(f"🔐 Общий порог доступа установлен: <b>{required} — {role_name(required)}</b>.")
        return
    if key not in {"mute","unmute","ban","unban","kick","warn","unwarn","promote","demote","purge","delete","pin","unpin"}:
        await message.answer(
            "Не удалось распознать команду. Склонения поддерживаются.\n"
            "Примеры: <code>.дк мутами 2</code>, <code>.дк за баны 4</code>, "
            "<code>.дк снять все предупреждения 3</code>, <code>.дк повысить рангом 2</code>."
        )
        return
    settings = await asyncio.to_thread(get_chat_settings, message.chat.id)
    settings[f"modrank_{key}"] = required
    save_chat_settings(message.chat.id, settings)
    await message.answer(
        f"🔐 Для команды <b>{escape(command_phrase)}</b> теперь нужен ранг "
        f"<b>{required} — {role_name(required)}</b>."
    )

@router.message(F.text.regexp(r"(?i)^[.!/]?(?:настройки фильтров|фильтры|filters)(?:\s|$)"))
async def filters_status_cmd(message: Message) -> None:
    settings = await asyncio.to_thread(get_chat_settings, message.chat.id)
    enabled = [name for name, key in FILTER_ALIASES.items() if settings.get(key)]
    await message.answer("🛡 <b>Фильтры чата</b>\n" + ("\n".join(f"• {name}: включён" for name in sorted(set(enabled))) if enabled else "Нет включённых фильтров.") + "\n\nВкл: <code>+антиссылка</code>; выкл: <code>-антиссылка</code>.")

@router.message(Command("settrigger"))
async def set_trigger_cmd(message: Message) -> None:
    if not await require_real_admin(message):
        return
    parts = args_after_command(message)
    if len(parts) < 2 or parts[0] not in {"3", "5", "7", "10"} or parts[1] not in {"mute", "ban", "off"}:
        await message.answer("Формат: <code>/settrigger 3 mute 1ч</code>, <code>/settrigger 7 ban</code> или <code>/settrigger 5 off</code>.")
        return
    threshold, action = parts[0], parts[1]
    if action == "mute" and len(parts) < 3:
        await message.answer("Для триггера mute укажи срок, например <code>/settrigger 3 mute 1ч</code>.")
        return
    settings = await asyncio.to_thread(get_chat_settings, message.chat.id)
    settings[f"warn_{threshold}_action"] = "off" if action == "off" else action
    if action != "off" and len(parts) >= 3:
        settings[f"warn_{threshold}_time"] = " ".join(parts[2:])
    save_chat_settings(message.chat.id, settings)
    await message.answer(f"✅ Триггер {threshold} варнов обновлён: {action}.")

@router.message(Command("filteraction"))
@router.message(F.text.regexp(r"(?i)^\\s*(?:фильтрдействие|антиспам действие)(?:\\s|$)"))
async def filter_action_cmd(message: Message) -> None:
    if not await require_real_admin(message):
        return
    raw = (message.text or "").strip()
    value = re.sub(
        r"(?i)^\\s*(?:/filteraction(?:@\\w+)?|[.!/]?(?:фильтрдействие|антиспам действие))\\s*",
        "", raw,
    ).strip().casefold()
    aliases = {
        "monitor": "monitor", "наблюдение": "monitor", "только лог": "monitor",
        "delete": "delete", "удалять": "delete", "удаление": "delete",
        "mute": "mute", "мут": "mute", "мутить": "mute",
    }
    action = aliases.get(value)
    if action is None:
        await message.answer(
            "Формат: <code>фильтрдействие monitor</code>, <code>фильтрдействие delete</code> "
            "или <code>фильтрдействие mute</code>. По умолчанию стоит monitor."
        )
        return
    settings = await asyncio.to_thread(get_chat_settings, message.chat.id, True)
    settings["filter_action"] = action
    saved = await asyncio.to_thread(db_upsert, "bot_chat_settings", {
        "chat_id": message.chat.id, "settings": dict(settings),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }, "chat_id")
    if not saved:
        await message.answer("⚠️ Не удалось сохранить настройку в Supabase; действие фильтра не изменено.")
        return
    _CHAT_SETTINGS_CACHE[message.chat.id] = dict(settings)
    _CHAT_SETTINGS_CACHE_TIME[message.chat.id] = time.monotonic()
    audit(message.chat.id, message.from_user.id if message.from_user else 0,
          "filter_action_changed", details=f"action={action}")
    labels = {"monitor": "только журналировать", "delete": "удалять обнаруженные сообщения", "mute": "выдавать временный мут"}
    await message.answer(f"✅ Действие антиспама: <b>{labels[action]}</b>.")


async def apply_configured_filter_action(message: Message, settings: dict[str, Any], reason: str) -> None:
    action = str(settings.get("filter_action") or "monitor").casefold()
    if action not in {"delete", "mute"} or not message.from_user:
        return
    try:
        member = await get_chat_member_cached(bot, message.chat.id, message.from_user.id)
        if member is None:
            log.warning("Skipped configured filter action because Telegram member status could not be verified chat=%s user=%s",
                        message.chat.id, message.from_user.id)
            return
        if member.status in (ChatMemberStatus.CREATOR, ChatMemberStatus.ADMINISTRATOR):
            log.info("Skipped configured filter action for Telegram admin chat=%s user=%s action=%s",
                     message.chat.id, message.from_user.id, action)
            return
        if action == "delete":
            await message.delete()
            audit(message.chat.id, 0, "spam_deleted", message.from_user.id,
                  f"reason={reason}; message_id={message.message_id}")
            return
        minutes = max(1, min(60, int(settings.get("filter_mute_minutes", 10) or 10)))
        await bot.restrict_chat_member(
            message.chat.id, message.from_user.id,
            permissions=ChatPermissions(
                can_send_messages=False, can_send_audios=False, can_send_documents=False,
                can_send_photos=False, can_send_videos=False, can_send_video_notes=False,
                can_send_voice_notes=False, can_send_polls=False, can_send_other_messages=False,
                can_add_web_page_previews=False,
            ),
            until_date=datetime.now(timezone.utc) + timedelta(minutes=minutes),
        )
        audit(message.chat.id, 0, "spam_muted", message.from_user.id,
              f"reason={reason}; minutes={minutes}; message_id={message.message_id}")
    except Exception:
        log.exception("Configured anti-spam action failed chat=%s user=%s action=%s reason=%s",
                      message.chat.id, message.from_user.id, action, reason)


@router.message(Command("premium", "pear_premium"))
@router.message(F.text.regexp(r"(?i)^\s*(?:премиум|pear premium)(?:\s|$)"))
async def premium_cmd(message: Message) -> None:
    """Chat-level Pear Premium switch managed by the bot owner/operators."""
    raw = (message.text or "").strip()
    parts = raw.split()
    action = parts[1].lower() if len(parts) > 1 else "info"
    settings = get_chat_settings(message.chat.id)
    enabled = bool(settings.get("pear_premium", False))

    if action in {"on", "вкл", "включить", "активировать", "enable"}:
        if not await _owner_or_operator(message):
            return
        settings["pear_premium"] = True
        save_chat_settings(message.chat.id, settings)
        audit(message.chat.id, message.from_user.id if message.from_user else 0,
              "premium_enable", None, "Pear Premium enabled")
        await message.answer(
            "💎 <b>Pear Premium активирован для этого чата!</b>\n\n"
            "Сохранена премиум-отметка чата и включён расширенный статус модерации.\n"
            "Настройки сохраняются в Supabase, если таблица bot_chat_settings настроена."
        )
        return
    if action in {"off", "выкл", "выключить", "отключить", "disable"}:
        if not await _owner_or_operator(message):
            return
        settings["pear_premium"] = False
        save_chat_settings(message.chat.id, settings)
        audit(message.chat.id, message.from_user.id if message.from_user else 0,
              "premium_disable", None, "Pear Premium disabled")
        await message.answer("💠 Pear Premium отключён для этого чата.")
        return
    if action != "info":
        await message.answer("Формат: <code>/premium</code>, <code>/premium on</code> или <code>/premium off</code>.")
        return
    status = "🟢 активен" if enabled else "⚪ не активирован"
    await message.answer(
        "💎 <b>Pear Premium</b>\n"
        f"Статус этого чата: <b>{status}</b>.\n\n"
        "Это внутренняя функция Pear CM, не платёжная подписка и не копия платных функций Iris.\n"
        "Овнер/оператор: <code>/premium on</code> — включить; <code>/premium off</code> — отключить."
    )


async def auto_moderation_filters(message: Message) -> None:
    """Detect configured spam rules; penalties stay off unless a real admin opts in."""
    if not message.from_user or message.from_user.is_bot or not is_group(message):
        return
    await track_activity(message)
    text_value = message.text or message.caption or ""
    if not text_value or re.match(r"^\s*(?:[/!.+-])", text_value):
        return
    if message.from_user.id == OWNER_ID:
        return
    if await asyncio.to_thread(_is_trusted_user, message.chat.id, message.from_user.id):
        return
    # Monitor-only: avoid per-message Telegram admin calls and blocking DB rank lookups.
    try:
        settings = await asyncio.to_thread(get_chat_settings, message.chat.id)
    except Exception:
        log.exception("Could not load monitor-only filter settings chat=%s", message.chat.id)
        return
    now = time.monotonic()
    key = (message.chat.id, message.from_user.id)
    normalized = re.sub(r"[^\w\s]", " ", text_value.lower(), flags=re.UNICODE)
    normalized = re.sub(r"\s+", " ", normalized).strip()
    reason = None

    if settings.get("antispam") and len(normalized) >= 12:
        history = _SPAM_HISTORY[key]
        while history and now - history[0][0] > 25:
            history.popleft()
        similar_count = 1
        for _stamp, old_text, _old_id in history:
            if old_text == normalized:
                similar_count += 1
            elif len(normalized) >= 24 and len(old_text) >= 24:
                try:
                    if difflib.SequenceMatcher(None, normalized, old_text).ratio() >= 0.94:
                        similar_count += 1
                except Exception:
                    pass
        history.append((now, normalized[:1000], message.message_id))
        if similar_count >= 3:
            reason = f"повторяющиеся/похожие сообщения: {similar_count} за 25 секунд"

    if settings.get("antiflood") and not reason:
        times = _FLOOD_TIMES[key]
        times.append(now)
        window = max(3, min(60, int(settings.get("flood_window", 10))))
        while times and now - times[0] > window:
            times.popleft()
        limit = max(8, min(30, int(settings.get("flood_limit", 8))))
        if len(times) >= limit:
            reason = f"возможный флуд: {len(times)} сообщений за {window} секунд"
            times.clear()

    if not reason and text_value:
        if settings.get("antiinvite") and INVITE_RE.search(text_value):
            reason = "пригласительная ссылка"
        elif settings.get("antilink") and URL_RE.search(text_value):
            reason = "ссылка"
        elif settings.get("antifishing") and FISHING_RE.search(text_value):
            reason = "подозрительная ссылка"
        elif settings.get("antimat") and any(word in text_value.lower() for word in BAD_WORDS):
            reason = "слова из фильтра антимата"
        elif settings.get("antireklama") and any(word in text_value.lower() for word in AD_WORDS):
            reason = "совпадение с фильтром рекламы"
        elif settings.get("antikripta") and any(word in text_value.lower() for word in ("bitcoin", "btc", "ethereum", "usdt", "крипта", "binance", "bybit", "кошелёк", "wallet")):
            reason = "совпадение с фильтром крипторекламы"
        elif settings.get("antiall") and re.search(r"@(all|everyone|here)\b", text_value, re.I):
            reason = "массовое упоминание"
        elif settings.get("anticaps"):
            letters = [c for c in text_value if c.isalpha()]
            if len(letters) >= 20 and sum(c.isupper() for c in letters) / len(letters) * 100 >= int(settings.get("caps_percent", 70)):
                reason = "чрезмерный капс"

    if not reason:
        return
    notice_key = (message.chat.id, message.from_user.id)
    last_notice = _SPAM_LAST.get(notice_key)
    if last_notice and now - last_notice[1] < 30:
        return
    _SPAM_LAST[notice_key] = (reason, now)
    audit(message.chat.id, 0, "spam_detected", message.from_user.id,
          f"monitor_only; reason={reason}; message_id={message.message_id}")
    log.info("Spam signal chat=%s user=%s reason=%s message=%s action=%s",
             message.chat.id, message.from_user.id, reason, message.message_id,
             settings.get("filter_action", "monitor"))
    # Penalties are disabled by default and run only after a real-admin explicitly configures them.
    await apply_configured_filter_action(message, settings, reason)

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
    rows = await asyncio.to_thread(db_rows, "bot_audit", limit=200, order="created_at", chat_id=message.chat.id)
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
    rows = await asyncio.to_thread(db_rows, "bot_audit", limit=200, order="created_at", chat_id=message.chat.id)
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
    row = await asyncio.to_thread(db_insert, "bot_reports", payload)
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
    row = await asyncio.to_thread(db_insert, "bot_appeals", {
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
    rows = await asyncio.to_thread(db_rows, "bot_reports", limit=20, order="created_at", chat_id=message.chat.id, status="open")
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
    rows = await asyncio.to_thread(db_rows, "bot_appeals", limit=20, order="created_at", chat_id=message.chat.id, status="open")
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
    settings = await asyncio.to_thread(get_chat_settings, message.chat.id)
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
    """Detect a possible raid, but never automatically restrict or delete anyone."""
    settings = await asyncio.to_thread(get_chat_settings, message.chat.id)
    if not settings.get("antiraid") or not message.new_chat_members:
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
    audit(message.chat.id, 0, "possible_raid_detected", None,
          f"monitor_only; joins={len(joins)}; threshold={threshold}; window={window}s")
    log.warning("Possible raid detected chat=%s joins=%s window=%s; no automatic restrictions applied",
                message.chat.id, len(joins), window)
    joins.clear()
    try:
        await message.answer(
            f"🛡 Обнаружена возможная волна вступлений ({threshold}+ за {window} сек.). "
            "Автоматические ограничения не применялись; проверьте участников вручную."
        )
    except Exception:
        pass




# ── Chat open/close commands (Russian Iris-style aliases) ────────────────────
_CHAT_ACTION_LOCKS: dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)

@router.message(Command("openchat", "unlockchat"))
@router.message(F.text.regexp(r"(?i)^\s*\+\s*чат\s*$"))
async def open_chat_cmd(message: Message) -> None:
    if not is_group(message):
        await message.answer("Команда работает только в группе.")
        return
    if not await require_real_admin(message):
        return
    async with _CHAT_ACTION_LOCKS[message.chat.id]:
        settings = await asyncio.to_thread(get_chat_settings, message.chat.id, True)
        if not settings.get("chat_locked_by_bot"):
            await message.answer("ℹ️ Чат уже открыт.")
            return
        saved = settings.get("chat_permissions_before_lock")
        try:
            if isinstance(saved, dict):
                permissions = ChatPermissions(**saved)
            else:
                permissions = ChatPermissions(
                    can_send_messages=True, can_send_audios=True,
                    can_send_documents=True, can_send_photos=True,
                    can_send_videos=True, can_send_video_notes=True,
                    can_send_voice_notes=True, can_send_polls=True,
                    can_send_other_messages=True, can_add_web_page_previews=True,
                )
            await bot.set_chat_permissions(message.chat.id, permissions=permissions)
            settings.pop("chat_permissions_before_lock", None)
            settings["chat_locked_by_bot"] = False
            save_chat_settings(message.chat.id, settings)
            persisted = False
            if db is not None:
                persisted = bool(await asyncio.to_thread(db_upsert, "bot_chat_settings", {
                    "chat_id": message.chat.id, "settings": dict(settings),
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                }, "chat_id"))
            audit(message.chat.id, message.from_user.id, "chat_open")
            if persisted:
                await message.answer("🔓 Чат открыт.")
            else:
                await message.answer("🔓 Чат открыт в Telegram, но состояние не удалось надёжно сохранить в базе. После восстановления базы повтори <code>+чат</code>.")
        except Exception as exc:
            await safe_error(message, exc)

@router.message(Command("closechat", "lockchat"))
@router.message(F.text.regexp(r"(?i)^\s*-\s*чат\s*$"))
async def close_chat_cmd(message: Message) -> None:
    if not is_group(message):
        await message.answer("Команда работает только в группе.")
        return
    if not await require_real_admin(message):
        return
    async with _CHAT_ACTION_LOCKS[message.chat.id]:
        settings = await asyncio.to_thread(get_chat_settings, message.chat.id, True)
        if settings.get("chat_locked_by_bot"):
            await message.answer("ℹ️ Чат уже закрыт. Открыть: <code>+чат</code>.")
            return
        try:
            if not settings.get("chat_permissions_before_lock"):
                # Save Telegram's actual default group permissions before locking.
                # Restoring a hard-coded "all True" set could accidentally enable media
                # types that the group owner had intentionally disabled.
                try:
                    chat_info = await bot.get_chat(message.chat.id)
                    current_permissions = getattr(chat_info, "permissions", None)
                    if current_permissions is not None:
                        settings["chat_permissions_before_lock"] = current_permissions.model_dump(exclude_none=True)
                except Exception:
                    log.exception("Could not read existing chat permissions before lock chat=%s", message.chat.id)
                if not settings.get("chat_permissions_before_lock"):
                    settings["chat_permissions_before_lock"] = {
                        "can_send_messages": True, "can_send_audios": True,
                        "can_send_documents": True, "can_send_photos": True,
                        "can_send_videos": True, "can_send_video_notes": True,
                        "can_send_voice_notes": True, "can_send_polls": True,
                        "can_send_other_messages": True, "can_add_web_page_previews": True,
                    }
            if db is None:
                await message.answer("⛔ Не закрываю чат: база данных не настроена, и после перезапуска бот может потерять сохранённые разрешения. Сначала подключи Supabase.")
                return
            settings["chat_locked_by_bot"] = True
            # Persist the restore permissions and lock marker BEFORE changing Telegram.
            # If the process exits between these steps, +чат safely restores the old permissions.
            pre_saved = await asyncio.to_thread(db_upsert, "bot_chat_settings", {
                "chat_id": message.chat.id, "settings": dict(settings),
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }, "chat_id")
            if not pre_saved:
                await message.answer("⛔ Не закрываю чат: не удалось сохранить исходные разрешения в базе.")
                return
            await bot.set_chat_permissions(
                message.chat.id,
                permissions=ChatPermissions(
                    can_send_messages=False, can_send_audios=False,
                    can_send_documents=False, can_send_photos=False,
                    can_send_videos=False, can_send_video_notes=False,
                    can_send_voice_notes=False, can_send_polls=False,
                    can_send_other_messages=False, can_add_web_page_previews=False,
                ),
            )
            save_chat_settings(message.chat.id, settings)
            audit(message.chat.id, message.from_user.id, "chat_close")
            await message.answer("🔒 Чат закрыт. Открыть: <code>+чат</code>.")
        except Exception as exc:
            await safe_error(message, exc)



# ── Advanced chat administration: temporary bans, notes, trusted users, settings ──
_TRUSTED_CACHE: dict[tuple[int, int], tuple[bool, float]] = {}
_TRUSTED_CACHE_TTL = 30.0

def _is_trusted_user(chat_id: int, user_id: int) -> bool:
    key = (chat_id, user_id)
    now = time.monotonic()
    cached = _TRUSTED_CACHE.get(key)
    if cached and cached[1] > now:
        return cached[0]
    row = db_one("bot_trusted_users", chat_id=chat_id, user_id=user_id, active=True)
    value = bool(row and row.get("active"))
    _TRUSTED_CACHE[key] = (value, now + _TRUSTED_CACHE_TTL)
    if len(_TRUSTED_CACHE) > 10000:
        for old_key, (_, expiry) in list(_TRUSTED_CACHE.items()):
            if expiry < now:
                _TRUSTED_CACHE.pop(old_key, None)
    return value

def _invalidate_trusted(chat_id: int, user_id: int) -> None:
    _TRUSTED_CACHE.pop((chat_id, user_id), None)

def _reason_from_preset(chat_id: int, action: str, raw_reason: str | None) -> str:
    reason = (raw_reason or "").strip()
    if not reason or reason.lower() in {"не указана", "не указано", "нет"}:
        return reason or "не указана"
    settings = get_chat_settings(chat_id)
    presets = settings.get("punishment_reasons", {})
    if not isinstance(presets, dict):
        return reason
    action_presets = presets.get(action, {})
    if not isinstance(action_presets, dict):
        return reason
    key = reason.removeprefix("причина:").removeprefix("причина=").strip().casefold()
    return str(action_presets.get(key, reason))[:500]

def _settings_exportable(settings: dict[str, Any]) -> dict[str, Any]:
    safe: dict[str, Any] = {}
    for key, value in settings.items():
        if key in FILTER_DEFAULTS or key.startswith(("modrank_", "warn_")) or key in {
            "welcome_message", "welcome_enabled", "schedule_enabled", "pear_premium", "schedule_open",
            "schedule_close", "schedule_timezone", "punishment_reasons", "command_cooldown",
            "flood_limit", "flood_window", "caps_percent", "filter_mute_minutes",
            "raid_threshold", "raid_window", "raid_restrict_minutes", "warn_3_action",
            "warn_3_time", "warn_5_action", "warn_5_time", "warn_7_action", "warn_7_time",
            "warn_10_action", "warn_10_time",
        }:
            safe[key] = value
    # Runtime lock snapshots are intentionally excluded from portable settings.
    safe.pop("chat_locked_by_bot", None)
    safe.pop("chat_permissions_before_lock", None)
    return safe

def _validate_imported_settings(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, dict) or len(raw) > 200:
        return None
    result: dict[str, Any] = {}
    for key, value in raw.items():
        if not isinstance(key, str) or len(key) > 80:
            return None
        allowed = key in FILTER_DEFAULTS or key.startswith(("modrank_", "warn_")) or key in {
            "welcome_message", "welcome_enabled", "schedule_enabled", "schedule_open",
            "schedule_close", "schedule_timezone", "punishment_reasons", "command_cooldown",
            "flood_limit", "flood_window", "caps_percent", "filter_mute_minutes",
            "raid_threshold", "raid_window", "raid_restrict_minutes",
        }
        if not allowed:
            continue
        if key.startswith("modrank_"):
            try:
                value = max(1, min(5, int(value)))
            except (TypeError, ValueError):
                return None
        elif key in {"welcome_enabled", "schedule_enabled"} or key in FILTER_DEFAULTS and isinstance(FILTER_DEFAULTS[key], bool):
            if not isinstance(value, bool):
                return None
        elif key == "punishment_reasons":
            if not isinstance(value, dict) or len(value) > 20:
                return None
            for action, items in value.items():
                if action not in {"mute", "ban", "kick", "warn", "unmute", "unban"} or not isinstance(items, dict) or len(items) > 50:
                    return None
                if any(not isinstance(k, str) or not isinstance(v, str) or len(k) > 40 or len(v) > 500 for k, v in items.items()):
                    return None
        elif key == "welcome_message":
            if not isinstance(value, str) or len(value) > 1500:
                return None
        elif key in {"schedule_open", "schedule_close"}:
            if not isinstance(value, str) or not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value):
                return None
        elif key == "schedule_timezone":
            if not isinstance(value, str) or len(value) > 80:
                return None
            try:
                ZoneInfo(value)
            except Exception:
                return None
        elif key.startswith("warn_"):
            if not isinstance(value, (str, int, bool)) or len(str(value)) > 100:
                return None
        elif isinstance(value, (int, float)):
            if abs(value) > 100000:
                return None
        elif isinstance(value, str):
            if len(value) > 500:
                return None
        elif not isinstance(value, (bool, list, dict)):
            return None
        result[key] = value
    return result

@router.message(Command("tempban", "врембан", "времан"))
@router.message(F.text.regexp(r"(?i)^\s*(?:врембан|времан|временный бан|tempban)(?:\s|$)"))
async def temporary_ban_cmd(message: Message) -> None:
    if not await check_mod(message, min_rank=3):
        return
    raw = (message.text or "").strip()
    rest = re.sub(r"(?i)^\s*(?:/tempban(?:@\w+)?|/врембан(?:@\w+)?|/времан(?:@\w+)?|врембан|времан|временный бан(?:\s+на)?|tempban)\s*", "", raw).strip()
    tokens = rest.split()
    if message.reply_to_message and message.reply_to_message.from_user:
        target_user = message.reply_to_message.from_user
        if target_user.is_bot:
            await message.answer("Ответьте на сообщение обычного участника.")
            return
        target = (target_user.id, f"@{target_user.username}" if target_user.username else target_user.full_name)
        duration_tokens = tokens
    else:
        if not tokens:
            await message.answer("Формат: ответьте на участника <code>врембан 1 день причина</code> или укажите <code>врембан @username 1 день причина</code>.")
            return
        if parse_duration_prefix(tokens) is not None:
            await message.answer("Сначала ответьте на сообщение участника или укажите цель: <code>врембан @username 1 день причина</code>.")
            return
        target = await resolve_target(message, tokens[:1])
        if not target:
            await message.answer("Не удалось найти пользователя. Ответьте на его сообщение или укажите известный чату @username/ID.")
            return
        duration_tokens = tokens[1:]
    parsed = parse_duration_prefix(duration_tokens)
    if not parsed:
        await message.answer("Укажите срок: <code>врембан 1 день</code>, <code>врембан день</code>, <code>врембан 1 месяц</code>, <code>врембан год</code> или <code>врембан 2ч причина</code>. Срок временного бана — не больше 365 дней. Ответьте на сообщение цели или укажите @username/ID.")
        return
    duration, consumed, duration_label = parsed
    reason = _reason_from_preset(message.chat.id, "ban", " ".join(duration_tokens[consumed:]).strip() or "не указана")
    until = datetime.now(timezone.utc) + duration
    try:
        target_member = await bot.get_chat_member(message.chat.id, target[0])
        if target_member.status in (ChatMemberStatus.CREATOR, ChatMemberStatus.ADMINISTRATOR):
            await message.answer("⛔ Нельзя временно забанить администратора или создателя Telegram-чата.")
            return
        await bot.ban_chat_member(message.chat.id, target[0], until_date=until)
        audit(message.chat.id, message.from_user.id if message.from_user else 0, "tempban", target[0],
              f"duration={duration_label}; reason={reason}; until={until.isoformat()}")
        actor = mention(message.from_user.id, message.from_user.full_name) if message.from_user else "модератор"
        await message.answer(
            f"⛔ {mention(target[0], target[1])} временно забанен на <b>{escape(duration_label)}</b>.\n"
            f"Причина: <b>{escape(reason[:300])}</b>\n"
            f"⏳ До: <code>{format_moscow_time(until)}</code>\n👮 Модератор: {actor}."
        )
    except Exception as exc:
        await safe_error(message, exc)

@router.message(Command("note", "заметка"))
@router.message(F.text.regexp(r"(?i)^\s*(?:заметка|заметки|/note)(?:\s|$)"))
async def admin_notes_cmd(message: Message) -> None:
    if not await require_admin(message, min_rank=2):
        return
    raw = (message.text or "").strip()
    match = re.match(r"(?i)^\s*(?:/note(?:@\w+)?|заметка|заметки)\s*(.*)$", raw)
    rest = (match.group(1) if match else "").strip()
    if not rest:
        if not message.reply_to_message or not message.reply_to_message.from_user:
            await message.answer("Форматы: ответьте на участника <code>заметка текст</code>; <code>заметки @username</code> — посмотреть заметки.")
            return
        target = message.reply_to_message.from_user
        rest = ""
    else:
        parts = rest.split()
        target = None
        if message.reply_to_message and message.reply_to_message.from_user:
            target = message.reply_to_message.from_user
        else:
            target = await resolve_target(message, parts[:1])
            if target:
                rest = rest[len(parts[0]):].strip()
        if not target:
            await message.answer("Укажите участника ответом на сообщение или через @username/ID.")
            return
    target_id = target.id if hasattr(target, "id") else target[0]
    target_name = (f"@{target.username}" if getattr(target, "username", None) else getattr(target, "full_name", None)) if hasattr(target, "id") else target[1]
    if rest:
        payload = {
            "user_id": int(target_id), "chat_id": int(message.chat.id),
            "actor_id": int(message.from_user.id if message.from_user else 0),
            "content": rest[:1500], "created_at": datetime.now(timezone.utc).isoformat(),
        }
        saved = await asyncio.to_thread(db_insert, "bot_notes", payload)
        if not saved:
            await message.answer("⚠️ Не удалось сохранить заметку. Проверь таблицу bot_notes и подключение базы.")
            return
        audit(message.chat.id, message.from_user.id if message.from_user else 0, "note_add", target_id, rest[:300])
        await message.answer(f"📝 Заметка для {mention(target_id, target_name)} сохранена.")
        return
    rows = await asyncio.to_thread(db_rows, "bot_notes", 100, "created_at", user_id=target_id, chat_id=message.chat.id)
    rows = [row for row in rows if row.get("chat_id") in (None, message.chat.id)]
    if not rows:
        await message.answer(f"📝 У {mention(target_id, target_name)} пока нет заметок в этом чате.")
        return
    lines = []
    for row in rows[:15]:
        content = escape(str(row.get("content") or "")[:300])
        stamp = escape(str(row.get("created_at") or ""))
        lines.append(f"• <code>#{row.get('id','?')}</code> · <code>{stamp}</code>\n  {content}")
    await message.answer(f"<b>📝 Заметки: {mention(target_id, target_name)}</b>\n" + "\n".join(lines))


@router.message(F.text.regexp(r"(?i)^\s*(?:удалить заметку|удалитьзаметку)\s+\d+\s*$"))
async def delete_admin_note_cmd(message: Message) -> None:
    if not await require_admin(message, min_rank=2):
        return
    match = re.search(r"(\d+)\s*$", message.text or "")
    if not match or db is None:
        await message.answer("Формат: <code>удалить заметку ID</code>.")
        return
    note_id = int(match.group(1))
    def delete_note_sync() -> bool:
        try:
            result = db.table("bot_notes").delete().eq("id", note_id).eq("chat_id", message.chat.id).execute()
            return bool(result.data)
        except Exception:
            log.exception("Could not delete admin note id=%s", note_id)
            return False
    deleted = await asyncio.to_thread(delete_note_sync)
    if deleted:
        audit(message.chat.id, message.from_user.id if message.from_user else 0, "note_delete", None, f"note_id={note_id}")
        await message.answer(f"🗑 Заметка <code>#{note_id}</code> удалена.")
    else:
        await message.answer("Заметка не найдена в этом чате или не удалось её удалить.")


@router.message(Command("trusted", "доверенные"))
@router.message(F.text.regexp(r"(?i)^\s*(?:доверенный|доверенные|недоверенный)(?:\s|$)"))
async def trusted_users_cmd(message: Message) -> None:
    if not await require_real_admin(message):
        return
    raw = (message.text or "").strip()
    match = re.match(r"(?i)^\s*(?:/trusted(?:@\w+)?|доверенный|доверенные|недоверенный)\s*(.*)$", raw)
    rest = (match.group(1) if match else "").strip()
    command = re.sub(r"(?i)^\s*(?:/trusted(?:@\w+)?|доверенный|доверенные|недоверенный)", "", raw).strip().lower()
    if not rest and not message.reply_to_message:
        rows = await asyncio.to_thread(db_rows, "bot_trusted_users", 100, "created_at", chat_id=message.chat.id, active=True)
        if not rows:
            await message.answer("🛡 Список доверенных участников пуст.")
            return
        lines = []
        for row in rows[:30]:
            uid = int(row.get("user_id") or 0)
            urow = await asyncio.to_thread(db_one, "bot_users", user_id=uid, chat_id=message.chat.id) or {}
            uname = ("@" + str(urow.get("username")).lstrip("@")) if urow.get("username") else str(urow.get("first_name") or f"ID {uid}")
            lines.append(f"• {escape(uname)} · <code>{uid}</code>")
        await message.answer("<b>🛡 Доверенные участники</b>\n" + "\n".join(lines))
        return
    target = None
    if message.reply_to_message and message.reply_to_message.from_user:
        u = message.reply_to_message.from_user
        target = (u.id, f"@{u.username}" if u.username else u.full_name)
    else:
        target = await resolve_target(message, rest.split()[:1])
    if not target:
        await message.answer("Ответьте на сообщение или укажите известный чату @username/ID.")
        return
    remove = raw.lower().lstrip().startswith("недоверенный")
    payload = {"chat_id": message.chat.id, "user_id": target[0], "active": not remove,
               "actor_id": message.from_user.id if message.from_user else 0,
               "created_at": datetime.now(timezone.utc).isoformat()}
    saved = await asyncio.to_thread(db_upsert, "bot_trusted_users", payload, "chat_id,user_id")
    if not saved:
        await message.answer("⚠️ Не удалось сохранить доверенный список. Проверь новую таблицу bot_trusted_users.")
        return
    _invalidate_trusted(message.chat.id, target[0])
    audit(message.chat.id, message.from_user.id if message.from_user else 0,
          "untrust" if remove else "trust", target[0])
    await message.answer(f"{'➖ Убран из доверенных' if remove else '🛡 Добавлен в доверенные'}: {mention(target[0], target[1])}.")

@router.message(Command("reasons", "причины"))
@router.message(F.text.regexp(r"(?i)^\s*(?:причины|причина)(?:\s|$)"))
async def punishment_reasons_cmd(message: Message) -> None:
    if not await require_admin(message, min_rank=2):
        return
    raw = (message.text or "").strip()
    match = re.match(r"(?i)^\s*(?:/reasons(?:@\w+)?|причины|причина)\s*(.*)$", raw)
    rest = (match.group(1) if match else "").strip()
    settings = await asyncio.to_thread(get_chat_settings, message.chat.id)
    presets = settings.get("punishment_reasons", {})
    if rest.lower().startswith("добавить "):
        parts = rest.split(maxsplit=3)
        if len(parts) < 4:
            await message.answer("Формат: <code>причина добавить мут спам Спам и ссылки запрещены</code>")
            return
        action_alias = parts[1].lower()
        action = {"мут":"mute", "бан":"ban", "кик":"kick", "варн":"warn", "предупреждение":"warn"}.get(action_alias, action_alias)
        key = parts[2].casefold()
        if action not in {"mute", "ban", "kick", "warn"} or not re.fullmatch(r"[\w-]{1,40}", key):
            await message.answer("Действие: мут, бан, кик или варн; ключ — одно короткое слово.")
            return
        if not isinstance(presets, dict):
            presets = {}
        action_presets = presets.setdefault(action, {})
        if not isinstance(action_presets, dict):
            action_presets = {}; presets[action] = action_presets
        action_presets[key] = parts[3][:500]
        settings["punishment_reasons"] = presets
        save_chat_settings(message.chat.id, settings)
        await message.answer(f"✅ Причина <code>{escape(key)}</code> для действия <b>{escape(action_alias)}</b> сохранена.")
        return
    if rest.lower().startswith("удалить "):
        parts = rest.split()
        if len(parts) != 3:
            await message.answer("Формат: <code>причина удалить мут спам</code>")
            return
        action = {"мут":"mute", "бан":"ban", "кик":"kick", "варн":"warn"}.get(parts[1].lower(), parts[1].lower())
        key = parts[2].casefold()
        if isinstance(presets, dict) and isinstance(presets.get(action), dict):
            presets[action].pop(key, None)
            settings["punishment_reasons"] = presets
            save_chat_settings(message.chat.id, settings)
        await message.answer(f"🗑 Шаблон причины <code>{escape(key)}</code> удалён, если существовал.")
        return
    lines = []
    if isinstance(presets, dict):
        for action, items in presets.items():
            if isinstance(items, dict):
                for key, value in items.items():
                    lines.append(f"• <b>{escape(action)}</b> · <code>{escape(str(key))}</code> — {escape(str(value)[:160])}")
    await message.answer(
        "<b>📚 Шаблоны причин наказаний</b>\n" +
        ("\n".join(lines[:40]) if lines else "Пока нет шаблонов.") +
        "\n\nДобавить: <code>причина добавить мут спам Текст причины</code>\nУдалить: <code>причина удалить мут спам</code>"
    )

@router.message(Command("modrights", "права"))
@router.message(F.text.regexp(r"(?i)^\s*права(?:\s|$)"))
async def granular_permissions_cmd(message: Message) -> None:
    if not await require_real_admin(message):
        return
    aliases = {
        "мут":"mute", "бан":"ban", "врембан":"ban", "кик":"kick", "варн":"warn",
        "анварн":"unwarn", "размут":"unmute", "разбан":"unban", "повысить":"promote",
        "понизить":"demote", "очистить":"purge", "-смс":"delete", "пин":"pin", "анпин":"unpin",
    }
    raw = (message.text or "").strip()
    rest = re.sub(r"(?i)^\s*(?:/modrights(?:@\w+)?|/права|права)\s*", "", raw).strip()
    if not rest:
        settings = await asyncio.to_thread(get_chat_settings, message.chat.id)
        lines = [f"• <b>{escape(key[7:])}</b>: ранг <b>{value}</b>" for key, value in settings.items()
                 if key.startswith("modrank_") and str(value).isdigit()]
        await message.answer("<b>🔐 Права команд</b>\n" + ("\n".join(lines) if lines else "Для всех команд действуют стандартные пороги.") +
                             "\n\nНастроить: <code>права мут 3</code> (ранг 1–5).")
        return
    parts = rest.split()
    if len(parts) != 2 or parts[0].lower() not in aliases or not parts[1].isdigit() or not 1 <= int(parts[1]) <= 5:
        await message.answer("Формат: <code>права мут 3</code>. Доступные действия: мут, бан, кик, варн, анварн, размут, разбан, повысить, понизить, очистить, пин.")
        return
    key, rank = aliases[parts[0].lower()], int(parts[1])
    settings = await asyncio.to_thread(get_chat_settings, message.chat.id)
    settings[f"modrank_{key}"] = rank
    save_chat_settings(message.chat.id, settings)
    audit(message.chat.id, message.from_user.id if message.from_user else 0, "permission_change", None, f"{key}={rank}")
    await message.answer(f"✅ Для команды <b>{escape(parts[0])}</b> установлен минимальный ранг <b>{rank} — {role_name(rank)}</b>.")

@router.message(Command("exportsettings", "экспортнастроек"))
@router.message(F.text.regexp(r"(?i)^\s*(?:экспорт настроек|экспорт настроек чата)\s*$"))
async def export_chat_settings_cmd(message: Message) -> None:
    if not await require_real_admin(message):
        return
    settings = await asyncio.to_thread(get_chat_settings, message.chat.id, True)
    data = {"format": "pear-cm-chat-settings", "version": 1, "settings": _settings_exportable(settings)}
    raw = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
    await message.answer_document(BufferedInputFile(raw, filename=f"pear_cm_settings_{message.chat.id}.json"),
                                 caption="Экспорт настроек Pear CM. Секреты и текущая блокировка чата не включены.")

@router.message(Command("importsettings", "импортнастроек"))
@router.message(F.text.regexp(r"(?i)^\s*(?:импорт настроек|импорт настроек чата)\s*$"))
async def import_chat_settings_cmd(message: Message) -> None:
    if not await require_real_admin(message):
        return
    doc = message.reply_to_message.document if message.reply_to_message else None
    if not doc:
        await message.answer("Сначала отправьте JSON-файл экспорта, затем ответьте на него командой <code>импорт настроек</code>.")
        return
    if doc.file_size and doc.file_size > 128 * 1024:
        await message.answer("Файл слишком большой. Максимальный размер — 128 КБ.")
        return
    try:
        buffer = BytesIO()
        await bot.download(doc.file_id, destination=buffer)
        parsed = json.loads(buffer.getvalue().decode("utf-8"))
        if not isinstance(parsed, dict) or parsed.get("format") != "pear-cm-chat-settings" or parsed.get("version") != 1:
            raise ValueError("неподдерживаемый формат")
        imported = _validate_imported_settings(parsed.get("settings"))
        if imported is None:
            raise ValueError("проверка значений не пройдена")
        current = await asyncio.to_thread(get_chat_settings, message.chat.id, True)
        current.update(imported)
        save_chat_settings(message.chat.id, current)
        audit(message.chat.id, message.from_user.id if message.from_user else 0, "settings_import", None, f"keys={len(imported)}")
        await message.answer(f"✅ Импортировано настроек: <b>{len(imported)}</b>. Права чата и секреты не импортировались.")
    except Exception as exc:
        log.warning("Settings import failed: %s", type(exc).__name__)
        await message.answer("⚠️ Не удалось импортировать JSON. Проверьте файл экспорта Pear CM и его целостность.")

@router.message(Command("welcome", "приветствие"))
@router.message(F.text.regexp(r"(?i)^\s*приветствие(?:\s|$)"))
async def configure_welcome_cmd(message: Message) -> None:
    if not await require_real_admin(message):
        return
    raw = (message.text or "").strip()
    rest = re.sub(r"(?i)^\s*(?:/welcome(?:@\w+)?|приветствие)\s*", "", raw).strip()
    settings = await asyncio.to_thread(get_chat_settings, message.chat.id)
    if not rest:
        enabled = settings.get("welcome_enabled", True)
        template = settings.get("welcome_message", "👋 Добро пожаловать, {name}!")
        await message.answer(f"<b>👋 Приветствие</b>: {'включено' if enabled else 'выключено'}\nШаблон: <code>{escape(str(template))}</code>\n\nУстановить: <code>приветствие Добро пожаловать, {{name}}!</code>\nВыключить: <code>приветствие выкл</code>\nВключить: <code>приветствие вкл</code>")
        return
    if rest.lower() in {"выкл", "выключить", "off", "disable"}:
        settings["welcome_enabled"] = False
    elif rest.lower() in {"вкл", "включить", "on", "enable"}:
        settings["welcome_enabled"] = True
    else:
        settings["welcome_enabled"] = True
        settings["welcome_message"] = rest[:1500]
    save_chat_settings(message.chat.id, settings)
    audit(message.chat.id, message.from_user.id if message.from_user else 0, "welcome_settings", None, rest[:200])
    await message.answer("✅ Настройки приветствия обновлены.")

@router.message(Command("modstats", "статистикамодераторов"))
@router.message(F.text.regexp(r"(?i)^\s*(?:статистика модераторов|статистика модераторов)(?:\s|$)"))
async def moderator_stats_cmd(message: Message) -> None:
    if not await require_admin(message, min_rank=2):
        return
    raw = (message.text or "").strip().lower()
    match = re.search(r"(день|сегодня|неделя|месяц|24ч|7д|30д)", raw)
    days = 1 if match and match.group(1) in {"день", "сегодня", "24ч"} else 7 if match and match.group(1) in {"неделя", "7д"} else 30
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    rows = await asyncio.to_thread(db_rows, "bot_audit", 1000, "created_at", chat_id=message.chat.id)
    counts: dict[int, int] = defaultdict(int)
    for row in rows:
        try:
            stamp = datetime.fromisoformat(str(row.get("created_at") or "").replace("Z", "+00:00"))
            if stamp.tzinfo is None:
                stamp = stamp.replace(tzinfo=timezone.utc)
            actor_id = int(row.get("actor_id") or 0)
        except Exception:
            continue
        if stamp >= cutoff and actor_id > 0:
            counts[actor_id] += 1
    if not counts:
        await message.answer(f"📊 За последние {days} дн. действий модераторов не найдено.")
        return
    lines = []
    for actor_id, count in sorted(counts.items(), key=lambda item: item[1], reverse=True)[:15]:
        user = await asyncio.to_thread(db_one, "bot_users", user_id=actor_id, chat_id=message.chat.id) or {}
        label = ("@" + str(user.get("username")).lstrip("@")) if user.get("username") else str(user.get("first_name") or f"ID {actor_id}")
        lines.append(f"• {escape(label)} — <b>{count}</b> действий")
    await message.answer(f"<b>📊 Статистика модераторов за {days} дн.</b>\n" + "\n".join(lines))

async def _apply_scheduled_chat_lock(chat_id: int, settings: dict[str, Any], should_lock: bool) -> bool:
    currently_locked = bool(settings.get("chat_locked_by_bot"))
    if currently_locked == should_lock:
        return False
    try:
        if should_lock:
            settings.setdefault("chat_permissions_before_lock", {
                "can_send_messages": True, "can_send_audios": True, "can_send_documents": True,
                "can_send_photos": True, "can_send_videos": True, "can_send_video_notes": True,
                "can_send_voice_notes": True, "can_send_polls": True, "can_send_other_messages": True,
                "can_add_web_page_previews": True,
            })
            await bot.set_chat_permissions(chat_id, permissions=ChatPermissions(
                can_send_messages=False, can_send_audios=False, can_send_documents=False,
                can_send_photos=False, can_send_videos=False, can_send_video_notes=False,
                can_send_voice_notes=False, can_send_polls=False, can_send_other_messages=False,
                can_add_web_page_previews=False,
            ))
            settings["chat_locked_by_bot"] = True
        else:
            saved = settings.get("chat_permissions_before_lock")
            permissions = ChatPermissions(**saved) if isinstance(saved, dict) else ChatPermissions(
                can_send_messages=True, can_send_audios=True, can_send_documents=True,
                can_send_photos=True, can_send_videos=True, can_send_video_notes=True,
                can_send_voice_notes=True, can_send_polls=True, can_send_other_messages=True,
                can_add_web_page_previews=True,
            )
            await bot.set_chat_permissions(chat_id, permissions=permissions)
            settings.pop("chat_permissions_before_lock", None)
            settings["chat_locked_by_bot"] = False
        audit(chat_id, 0, "scheduled_chat_close" if should_lock else "scheduled_chat_open")
        return True
    except Exception:
        log.exception("Scheduled chat lock change failed chat=%s lock=%s", chat_id, should_lock)
        return False

async def _schedule_worker() -> None:
    """Apply configured daily chat open/close times. A per-day marker prevents duplicate actions."""
    while True:
        try:
            rows = await asyncio.to_thread(db_rows, "bot_chat_settings", 1000, "updated_at")
            for row in rows:
                try:
                    chat_id = int(row.get("chat_id"))
                    settings = row.get("settings") if isinstance(row.get("settings"), dict) else {}
                    if not settings.get("schedule_enabled"):
                        continue
                    tz = ZoneInfo(str(settings.get("schedule_timezone") or "Europe/Moscow"))
                    now_local = datetime.now(tz)
                    hhmm = now_local.strftime("%H:%M")
                    date_key = now_local.strftime("%Y-%m-%d")
                    for mode, key, lock in (("close", "schedule_close", True), ("open", "schedule_open", False)):
                        if str(settings.get(key) or "") != hhmm or settings.get(f"_schedule_{mode}_date") == date_key:
                            continue
                        changed = await _apply_scheduled_chat_lock(chat_id, settings, lock)
                        settings[f"_schedule_{mode}_date"] = date_key
                        if changed:
                            try:
                                await bot.send_message(chat_id, "🔒 Чат автоматически закрыт по расписанию." if lock else "🔓 Чат автоматически открыт по расписанию.")
                            except Exception:
                                pass
                        save_chat_settings(chat_id, settings)
                except Exception:
                    log.exception("Invalid chat schedule record")
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Schedule worker failed")
        await asyncio.sleep(30)

@router.message(Command("schedule", "расписание"))
@router.message(F.text.regexp(r"(?i)^\s*расписание(?:\s|$)"))
async def chat_schedule_cmd(message: Message) -> None:
    if not await require_real_admin(message):
        return
    raw = (message.text or "").strip()
    rest = re.sub(r"(?i)^\s*(?:/schedule(?:@\w+)?|расписание)\s*", "", raw).strip()
    settings = await asyncio.to_thread(get_chat_settings, message.chat.id)
    if not rest:
        enabled = bool(settings.get("schedule_enabled"))
        await message.answer(
            f"<b>🕒 Расписание чата</b>: {'включено' if enabled else 'выключено'}\n"
            f"Закрытие: <code>{escape(str(settings.get('schedule_close') or 'не задано'))}</code>\n"
            f"Открытие: <code>{escape(str(settings.get('schedule_open') or 'не задано'))}</code>\n"
            f"Часовой пояс: <code>{escape(str(settings.get('schedule_timezone') or 'Europe/Moscow'))}</code>\n\n"
            "Настроить: <code>расписание 23:00 08:00</code>\nОтключить: <code>расписание выкл</code>"
        )
        return
    if rest.lower() in {"выкл", "выключить", "off"}:
        settings["schedule_enabled"] = False
        save_chat_settings(message.chat.id, settings)
        await message.answer("🛑 Автоматическое расписание выключено.")
        return
    parts = rest.split()
    if len(parts) not in (2, 3) or not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", parts[0]) or not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", parts[1]):
        await message.answer("Формат: <code>расписание 23:00 08:00</code> или <code>расписание 23:00 08:00 Europe/Moscow</code>.")
        return
    tz_name = parts[2] if len(parts) == 3 else "Europe/Moscow"
    try:
        ZoneInfo(tz_name)
    except Exception:
        await message.answer("Неизвестный часовой пояс. Пример: <code>Europe/Moscow</code>.")
        return
    settings.update({"schedule_enabled": True, "schedule_close": parts[0], "schedule_open": parts[1], "schedule_timezone": tz_name})
    settings.pop("_schedule_close_date", None)
    settings.pop("_schedule_open_date", None)
    save_chat_settings(message.chat.id, settings)
    audit(message.chat.id, message.from_user.id if message.from_user else 0, "schedule_settings", None, f"close={parts[0]};open={parts[1]};tz={tz_name}")
    await message.answer(f"✅ Расписание включено: закрытие <b>{parts[0]}</b>, открытие <b>{parts[1]}</b> ({escape(tz_name)}).")


router.message(F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}), F.from_user)(auto_moderation_filters)


# ── Owner console + community utilities (increment 2) ────────────────────────
_PROCESS_STARTED = time.monotonic()
_RUNTIME_FLAGS: dict[str, Any] = {"maintenance": False}
_GLOBAL_SETTINGS_CACHE: dict[str, tuple[Any, float]] = {}
_GLOBAL_BAN_CACHE: dict[int, tuple[bool, float]] = {}
_GLOBAL_BAN_NOTICE_LAST: dict[int, float] = {}
_GLOBAL_SETTING_TTL = 5.0
_GLOBAL_BAN_TTL = 10.0

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
    "/operators — список операторов\n/globalban ID|@username [срок] причина — глобальный запрет\n"
    "/globalunban ID|@username — снять глобальный запрет\n"
    "/ban ID|@username [срок] причина — бан оператора/овнера в личке\n"
    "/unban ID|@username — разбан оператора/овнера в личке\n/audit ID — аудит действий по ID\n\n"
    "<i>Панель овнера доступна только BOT_OWNER_ID; операторы могут модерировать, но не управлять овнерами.</i>"
)

def _global_setting(key: str, default: Any = None) -> Any:
    now = time.monotonic()
    cached = _GLOBAL_SETTINGS_CACHE.get(key)
    if cached and cached[1] > now:
        return cached[0]
    row = db_one("bot_global_settings", key=key)
    value = row.get("value", default) if row is not None else _RUNTIME_FLAGS.get(key, default)
    _GLOBAL_SETTINGS_CACHE[key] = (value, now + _GLOBAL_SETTING_TTL)
    return value

def _set_global_setting(key: str, value: Any) -> bool:
    _RUNTIME_FLAGS[key] = value
    _GLOBAL_SETTINGS_CACHE[key] = (value, time.monotonic() + _GLOBAL_SETTING_TTL)
    row = db_upsert("bot_global_settings", {
        "key": key, "value": str(value),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }, "key")
    return row is not None

def _is_globally_banned(user_id: int) -> bool:
    now = time.monotonic()
    cached = _GLOBAL_BAN_CACHE.get(user_id)
    if cached and cached[1] > now:
        return cached[0]
    row = db_one("bot_global_bans", user_id=user_id, active=True)
    value = bool(row and row.get("active"))
    # Timed global bans expire persistently, even after the bot restarts.
    if value and row:
        raw_expiry = row.get("expires_at")
        if raw_expiry:
            try:
                expiry = datetime.fromisoformat(str(raw_expiry).replace("Z", "+00:00"))
                if expiry.tzinfo is None:
                    expiry = expiry.replace(tzinfo=timezone.utc)
                if expiry <= datetime.now(timezone.utc):
                    db_upsert("bot_global_bans", {
                        "user_id": user_id, "active": False,
                        "reason": row.get("reason") or "", "actor_id": row.get("actor_id"),
                        "created_at": row.get("created_at") or datetime.now(timezone.utc).isoformat(),
                        "expires_at": raw_expiry,
                    }, "user_id")
                    value = False
            except (TypeError, ValueError):
                log.warning("Invalid global-ban expiry for user_id=%s", user_id)
    _GLOBAL_BAN_CACHE[user_id] = (value, now + _GLOBAL_BAN_TTL)
    if len(_GLOBAL_BAN_CACHE) > 20000:
        for cached_id, (_, expires_at) in list(_GLOBAL_BAN_CACHE.items()):
            if expires_at <= now:
                _GLOBAL_BAN_CACHE.pop(cached_id, None)
        if len(_GLOBAL_BAN_CACHE) > 20000:
            for cached_id in list(_GLOBAL_BAN_CACHE)[:5000]:
                _GLOBAL_BAN_CACHE.pop(cached_id, None)
    return value

def _operator_active(user_id: int) -> bool:
    row = db_one("bot_operators", user_id=user_id, active=True)
    return bool(row and row.get("active"))

async def _owner_only(message: Message) -> bool:
    if message.from_user and message.from_user.id == OWNER_ID:
        return True
    await message.answer("⛔ Эта команда доступна только овнеру бота.")
    return False

async def _owner_or_operator(message: Message) -> bool:
    if not message.from_user:
        await message.answer("⛔ Нужен доступ овнера или активного оператора.")
        return False
    if message.from_user.id == OWNER_ID:
        return True
    # Supabase lookup must not stall the asyncio update loop.
    if await asyncio.to_thread(_operator_active, message.from_user.id):
        return True
    await message.answer("⛔ Нужен доступ овнера или активного оператора.")
    return False

@router.message(Command("owner", "ownerpanel"))
@router.message(F.text.regexp(r"(?i)^\s*(?:овнер панель|панель овнера|админка овнера|админ-панель)\s*$"))
async def owner_panel(message: Message) -> None:
    # Never open or mention the owner panel from a group or supergroup.
    if message.chat.type != ChatType.PRIVATE:
        return
    if not await _owner_or_operator(message):
        return
    await message.answer(
        "🛠 <b>Панель управления Pear CM</b>\n\nВыберите действие кнопкой ниже. "
        "Для бана бот попросит прислать ID или @username, а затем срок и причину. "
        "Доступ только владельцу и активным операторам.",
        reply_markup=admin_inline_keyboard(),
    )

_ADMIN_INPUT_STATE: dict[int, str] = {}

@router.callback_query(F.data.startswith("home:"))
async def home_inline_callback(callback: CallbackQuery) -> None:
    if not callback.message:
        await callback.answer()
        return
    action = (callback.data or "").split(":", 1)[1]
    synthetic = callback.message.model_copy(update={"from_user": callback.from_user})
    if action == "start":
        await callback.message.answer(
            "👋 <b>Pear CM</b> управляет группой: модерация, статистика, фильтры и инструменты администраторов.",
            reply_markup=welcome_inline_keyboard(callback.from_user.id, callback.message.chat.type),
        )
    elif action == "help":
        synthetic = synthetic.model_copy(update={"text": "🔗 Команды"})
        await help_command(synthetic)
    elif action == "profile":
        synthetic = synthetic.model_copy(update={"text": "💠 Профиль"})
        await profile(synthetic)
    await callback.answer()

@router.callback_query(F.data.startswith("menu:"))
async def inline_menu_callback(callback: CallbackQuery) -> None:
    if not callback.message:
        await callback.answer()
        return
    action = (callback.data or "").split(":", 1)[1]
    synthetic = callback.message.model_copy(update={"from_user": callback.from_user})
    handlers = {
        "marriage": ("💍 Брак", marriage_menu),
        "installation": ("🛠 Установка", installation_help),
        "warns": ("⚠️ Мои варны", my_warns),
        "admins": ("🛡 Кто админ", admins),
    }
    item = handlers.get(action)
    if item:
        synthetic = synthetic.model_copy(update={"text": item[0]})
        await item[1](synthetic)
    await callback.answer()

@router.callback_query(F.data.startswith("admin:"))
async def admin_inline_callback(callback: CallbackQuery) -> None:
    if not callback.from_user or not callback.message:
        await callback.answer("Не удалось определить пользователя.", show_alert=True)
        return
    if callback.message.chat.type != ChatType.PRIVATE:
        await callback.answer("Админ-панель доступна только в личных сообщениях с ботом.", show_alert=True)
        return
    if callback.from_user.id != OWNER_ID and not await asyncio.to_thread(_operator_active, callback.from_user.id):
        await callback.answer("Нет доступа к админ-панели.", show_alert=True)
        return
    action = (callback.data or "").split(":", 1)[1]
    if action == "open":
        await callback.message.answer("🛠 <b>Админ-панель Pear CM</b>\nВыберите действие:", reply_markup=admin_inline_keyboard())
    elif action in {"ban", "unban"}:
        _ADMIN_INPUT_STATE[callback.from_user.id] = action
        verb = "бан" if action == "ban" else "разбан"
        await callback.message.answer(
            f"Введите одним сообщением: <code>@username или Telegram_ID [срок] причина</code> для действия «{verb}».\n"
            "Примеры: <code>@username 1 день спам</code>, <code>123456789 навсегда мошенничество</code>.\n"
            "Для разбана достаточно ID или @username. Username должен быть уже известен боту.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена", callback_data="admin:cancel")]])
        )
    elif action == "cancel":
        _ADMIN_INPUT_STATE.pop(callback.from_user.id, None)
        await callback.message.answer("Действие отменено.", reply_markup=admin_inline_keyboard())
    elif action == "operators":
        await callback.message.answer("Управление операторами: /operators, /operator ID, /revokeoperator ID. Назначайте операторов только тем, кому доверяете.", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="📋 Показать операторов", callback_data="admin:operators_list")],[InlineKeyboardButton(text="⬅️ Назад", callback_data="admin:open")]]))
    elif action == "operators_list":
        synthetic = callback.message.model_copy(update={"from_user": callback.from_user, "text": "/operators"})
        await owner_list_operators(synthetic)
        await callback.message.answer("🛠 Админ-панель:", reply_markup=admin_inline_keyboard())
    elif action == "globalstats":
        synthetic = callback.message.model_copy(update={"from_user": callback.from_user, "text": "/globalstats"})
        await owner_global_stats(synthetic)
        await callback.message.answer("🛠 Админ-панель:", reply_markup=admin_inline_keyboard())
    elif action == "logs":
        synthetic = callback.message.model_copy(update={"from_user": callback.from_user, "text": "/logs"})
        await owner_logs(synthetic)
        await callback.message.answer("🛠 Админ-панель:", reply_markup=admin_inline_keyboard())
    elif action == "modules":
        synthetic = callback.message.model_copy(update={"from_user": callback.from_user, "text": "/modules"})
        await owner_modules(synthetic)
        await callback.message.answer("🛠 Админ-панель:", reply_markup=admin_inline_keyboard())
    elif action == "maintenance":
        await callback.message.answer("Режим обслуживания: отправьте <code>/maintenance on</code> или <code>/maintenance off</code>. Это глобально ограничивает функции бота, поэтому изменение требует явной команды.", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад", callback_data="admin:open")]]))
    elif action == "filters":
        await callback.message.answer("Фильтры текущей группы: <code>фильтры</code>, <code>+антиссылка</code>, <code>-антиссылка</code>, <code>+антифлуд</code>, <code>-антифлуд</code>. Настройки применяются к группе, где вы их вводите.", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад", callback_data="admin:open")]]))
    elif action == "stats":
        rows = await asyncio.to_thread(db_rows, "bot_users", limit=1000)
        await callback.message.answer(f"📊 <b>Статистика</b>\nЗаписей пользователей в выборке: <b>{len(rows)}</b>.", reply_markup=admin_inline_keyboard())
    elif action == "status":
        synthetic = callback.message.model_copy(update={"from_user": callback.from_user, "text": "/status"})
        await owner_status(synthetic)
        await callback.message.answer("🛠 Админ-панель:", reply_markup=admin_inline_keyboard())
    elif action == "diag":
        synthetic = callback.message.model_copy(update={"from_user": callback.from_user, "text": "/diag"})
        await owner_diagnostics(synthetic)
        await callback.message.answer("🛠 Админ-панель:", reply_markup=admin_inline_keyboard())
    elif action == "home":
        await callback.message.answer("Главное меню:", reply_markup=welcome_inline_keyboard(callback.from_user.id))
    await callback.answer()

@router.message(F.chat.type == ChatType.PRIVATE, F.text, lambda message: bool(message.from_user and message.from_user.id in _ADMIN_INPUT_STATE))
async def admin_inline_input_handler(message: Message) -> None:
    if not message.from_user:
        return
    action = _ADMIN_INPUT_STATE.get(message.from_user.id)
    if not action:
        return
    if message.from_user.id != OWNER_ID and not await asyncio.to_thread(_operator_active, message.from_user.id):
        _ADMIN_INPUT_STATE.pop(message.from_user.id, None)
        return
    prefix = "/ban" if action == "ban" else "/unban"
    synthetic = message.model_copy(update={"text": f"{prefix} {message.text.strip()}"})
    _ADMIN_INPUT_STATE.pop(message.from_user.id, None)
    await _global_moderation_command(synthetic, remove=(action == "unban"))
    await message.answer("🛠 Панель управления:", reply_markup=admin_inline_keyboard())

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
    users, maintenance = await asyncio.gather(
        asyncio.to_thread(db_rows, "bot_users", limit=1000),
        asyncio.to_thread(_global_setting, "maintenance", "off"),
    )
    await message.answer(
        "<b>📡 Статус Pear CM</b>\n"
        f"Telegram API: <code>{telegram_state}</code>\n"
        f"Процесс: <b>работает</b> · uptime <code>{h}ч {m}м {s}с</code>\n"
        f"База: <b>{'настроена' if db is not None else 'не настроена'}</b>\n"
        f"Записей пользователей в выборке: <b>{len(users)}</b>\n"
        f"Техобслуживание: <b>{'включено' if str(maintenance).lower() == 'on' else 'выключено'}</b>\n"
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
        for table in ("bot_users", "bot_audit", "bot_chat_settings", "bot_global_settings", "bot_operators", "bot_global_bans", "bot_trusted_users"):
            try:
                db.table(table).select("*").limit(1).execute()
                lines.append(f"• {table}: <b>OK</b>")
            except Exception:
                lines.append(f"• {table}: <b>нет доступа/таблица не создана</b>")
    recent_latencies = [elapsed for elapsed, _kind in list(_COMMAND_LATENCIES)[-100:]]
    if recent_latencies:
        ordered = sorted(recent_latencies)
        p95 = ordered[min(len(ordered) - 1, int(len(ordered) * 0.95))]
        lines.append(f"• Обработчики: p50={ordered[len(ordered)//2]*1000:.0f} мс; p95={p95*1000:.0f} мс (последние {len(ordered)} обновлений)")
    await message.answer("<b>🧪 Диагностика</b>\n" + "\n".join(lines) + "\n\nСекретные значения не выводятся.")

@router.message(Command("globalstats", "глобальнаястатистика"))
@router.message(F.text.regexp(r"(?i)^\s*глобальная статистика\s*$"))
async def owner_global_stats(message: Message) -> None:
    if not await _owner_or_operator(message):
        return
    rows = await asyncio.to_thread(db_rows, "bot_users", limit=1000)
    audits = await asyncio.to_thread(db_rows, "bot_audit", limit=1000)
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
    rows = await asyncio.to_thread(db_rows, "bot_audit", limit=count, order="created_at")
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
    rows=await asyncio.to_thread(db_rows, "bot_audit", limit=20, order="created_at", target_id=uid)
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
        value=str(await asyncio.to_thread(_global_setting, "maintenance", "off")).lower()
    else:
        await message.answer("Формат: <code>/maintenance on</code> или <code>/maintenance off</code>.")
        return
    if action not in {"status", "статус"}:
        persisted = await asyncio.to_thread(_set_global_setting, "maintenance", value)
        if not persisted:
            await message.answer("⚠️ Флаг техобслуживания изменён в памяти, но не подтверждён базой. Проверь таблицу bot_global_settings.")
            return
        await audit_wait(message.chat.id, message.from_user.id, "maintenance", None, value)
    await message.answer(f"🛠 Техобслуживание: <b>{'включено' if value == 'on' else 'выключено'}</b>.")

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
    row=await asyncio.to_thread(db_upsert, "bot_operators", {"user_id":uid,"active":True,"created_by":OWNER_ID,"created_at":datetime.now(timezone.utc).isoformat()}, "user_id")
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
    row=await asyncio.to_thread(db_upsert, "bot_operators", {"user_id":uid,"active":False,"created_by":OWNER_ID,"created_at":datetime.now(timezone.utc).isoformat()}, "user_id")
    if row:
        audit(message.chat.id, OWNER_ID, "operator_revoke", uid, "operator revoked")
        await message.answer(f"🔒 Доступ ID <code>{uid}</code> отозван.")
    else: await message.answer("Не удалось обновить доступ. Проверь таблицу bot_operators в Supabase.")

@router.message(Command("operators", "операторы"))
async def owner_list_operators(message: Message) -> None:
    if not await _owner_only(message): return
    rows=await asyncio.to_thread(db_rows, "bot_operators", limit=100, order="created_at")
    if not rows:
        await message.answer("Список пуст или таблица bot_operators не создана."); return
    await message.answer("<b>Операторы</b>\n" + "\n".join(f"• <code>{r.get('user_id')}</code> — {'активен' if r.get('active') else 'отозван'}" for r in rows))

def _find_known_user_by_username(username: str) -> dict | None:
    """Resolve only usernames the bot has observed; Telegram cannot enumerate arbitrary users by username."""
    if db is None:
        return None
    try:
        result = db.table("bot_users").select("user_id,username,first_name").ilike(
            "username", username.lstrip("@")
        ).limit(1).execute()
        rows = result.data or []
        return rows[0] if rows else None
    except Exception:
        log.exception("Could not resolve known username for global moderation")
        return None

async def _resolve_global_target(token: str) -> tuple[int, str] | None:
    token = (token or "").strip().rstrip(",")
    if re.fullmatch(r"\d{1,20}", token):
        uid = int(token)
        row = await asyncio.to_thread(db_one, "bot_users", user_id=uid)
        username = (row or {}).get("username")
        name = f"@{username}" if username else f"ID {uid}"
        return uid, name
    username = token.lstrip("@")
    if not re.fullmatch(r"[A-Za-z0-9_]{5,32}", username):
        return None
    row = await asyncio.to_thread(_find_known_user_by_username, username)
    if not row or not row.get("user_id"):
        return None
    return int(row["user_id"]), f"@{row.get('username') or username}"

async def _notify_global_action(message: Message, uid: int, target_label: str,
                                action: str, reason: str = "", duration_label: str | None = None,
                                expires_at: str | None = None) -> tuple[bool, bool]:
    """Notify target and owner; Telegram may reject DMs if the target never started the bot."""
    if action == "ban":
        duration_text = f" на <b>{escape(duration_label)}</b>" if duration_label else " <b>навсегда</b>"
        user_text = (
            f"⛔ <b>Доступ к Pear CM ограничен{duration_text}.</b>\n"
            f"Причина: {escape(reason or 'причина не указана')}\n"
            "Если считаете это ошибкой, свяжитесь с владельцем бота."
        )
        owner_text = (
            f"🚫 <b>Глобальный бан выдан</b>\nПользователь: {escape(target_label)} "
            f"(<code>{uid}</code>)\nСрок:{duration_text}\nПричина: {escape(reason or 'причина не указана')}\n"
            f"Кто выдал: <code>{message.from_user.id if message.from_user else 0}</code>"
        )
    else:
        user_text = "✅ <b>Глобальный запрет Pear CM снят.</b> Вы снова можете пользоваться ботом."
        owner_text = (
            f"✅ <b>Глобальный бан снят</b>\nПользователь: {escape(target_label)} "
            f"(<code>{uid}</code>)\nКто снял: <code>{message.from_user.id if message.from_user else 0}</code>"
        )
    user_notified = False
    owner_notified = False
    try:
        await bot.send_message(uid, user_text)
        user_notified = True
    except Exception as exc:
        log.info("Could not DM global moderation notice to user %s: %s", uid, type(exc).__name__)
    actor_id = message.from_user.id if message.from_user else 0
    # The command response itself informs the owner if they issued it in their private chat.
    if not (actor_id == OWNER_ID and message.chat.type == ChatType.PRIVATE and message.chat.id == OWNER_ID):
        try:
            await bot.send_message(OWNER_ID, owner_text)
            owner_notified = True
        except Exception as exc:
            log.warning("Could not DM owner global moderation notice: %s", type(exc).__name__)
    return user_notified, owner_notified

async def _global_moderation_command(message: Message, remove: bool = False) -> None:
    raw = (message.text or "").strip()
    match = re.match(
        r"(?is)^/(?:globalban|globanban|globalunban|globanunban|ban|unban)(?:@\w+)?\s*(.*)$",
        raw,
    )
    if match:
        rest = match.group(1).strip()
    else:
        natural = re.match(r"(?is)^\s*глобальный\s+(?:бан|разбан)\s+(.*)$", raw)
        rest = natural.group(1).strip() if natural else ""
    parts = rest.split()
    if not parts:
        verb = "/unban" if remove else "/ban"
        await message.answer(f"Формат: <code>{verb} ID|@username [срок] причина</code>")
        return
    resolved = await _resolve_global_target(parts[0])
    if not resolved:
        await message.answer(
            "❌ Не удалось найти пользователя. Укажи числовой Telegram ID или @username, "
            "который бот уже встречал в чате/базе. Telegram не позволяет боту искать любого человека по username."
        )
        return
    uid, target_label = resolved
    if uid == OWNER_ID:
        await message.answer("⛔ Нельзя заблокировать или разбанить овнера через модерацию.")
        return
    actor_id = message.from_user.id if message.from_user else 0
    tail = parts[1:]
    now_iso = datetime.now(timezone.utc).isoformat()
    if remove:
        row = await asyncio.to_thread(db_upsert, "bot_global_bans", {
            "user_id": uid, "active": False, "reason": "", "actor_id": actor_id,
            "created_at": now_iso, "expires_at": None,
        }, "user_id")
        if not row:
            await message.answer("❌ Не удалось снять бан. Проверь Supabase и выполни миграцию из schema.sql.")
            return
        _GLOBAL_BAN_CACHE[uid] = (False, time.monotonic() + _GLOBAL_BAN_TTL)
        await asyncio.to_thread(audit, message.chat.id, actor_id, "globalunban", uid, "global ban removed")
        user_notified, owner_notified = await _notify_global_action(message, uid, target_label, "unban")
        notice = "Уведомление пользователю отправлено." if user_notified else "Пользователь не получил личное уведомление (возможно, не запускал бота)."
        await message.answer(f"✅ Глобальный бан снят: {escape(target_label)} (<code>{uid}</code>).\n{notice}")
        return

    forever_words = {"навсегда", "бессрочно", "перманентно", "постоянно", "вечный", "вечная", "вечное"}
    duration: timedelta | None = None
    duration_label: str | None = None
    if tail and tail[0].lower().strip(",.;:") in forever_words:
        reason = " ".join(tail[1:]).strip(" ,.;:-") or "причина не указана"
    else:
        parsed = parse_duration_prefix(tail) if tail else None
        if parsed:
            duration, consumed, duration_label = parsed
            reason = " ".join(tail[consumed:]).strip(" ,.;:-") or "причина не указана"
        else:
            reason = " ".join(tail).strip(" ,.;:-") or "причина не указана"
    expires_at = (datetime.now(timezone.utc) + duration).isoformat() if duration else None
    row = await asyncio.to_thread(db_upsert, "bot_global_bans", {
        "user_id": uid, "active": True, "reason": reason, "actor_id": actor_id,
        "created_at": now_iso, "expires_at": expires_at,
    }, "user_id")
    if not row:
        await message.answer("❌ Не удалось сохранить глобальный бан. Проверь Supabase и выполни миграцию из schema.sql.")
        return
    _GLOBAL_BAN_CACHE[uid] = (True, time.monotonic() + _GLOBAL_BAN_TTL)
    await asyncio.to_thread(audit, message.chat.id, actor_id, "globalban", uid, reason)
    user_notified, owner_notified = await _notify_global_action(
        message, uid, target_label, "ban", reason, duration_label, expires_at
    )
    term = f"на <b>{escape(duration_label)}</b>" if duration_label else "<b>навсегда</b>"
    user_notice = "Личное уведомление пользователю отправлено." if user_notified else "Пользователь не получил личное уведомление (возможно, не запускал бота)."
    await message.answer(
        f"🚫 Глобальный запрет включён: {escape(target_label)} (<code>{uid}</code>) {term}.\n"
        f"Причина: {escape(reason)}\n{user_notice}\n"
        "<i>Это блокировка функций Pear CM, не бан во всех Telegram-группах.</i>"
    )

@router.message(Command("globalban", "globanban"))
@router.message(F.text.regexp(r"(?i)^\s*глобальный\s+бан(?:\s+|$)"))
async def owner_global_ban(message: Message) -> None:
    if not await _owner_or_operator(message):
        return
    await _global_moderation_command(message, remove=False)

@router.message(Command("globalunban", "globanunban"))
@router.message(F.text.regexp(r"(?i)^\s*глобальный\s+разбан(?:\s+|$)"))
async def owner_global_unban(message: Message) -> None:
    if not await _owner_or_operator(message):
        return
    await _global_moderation_command(message, remove=True)

@router.message(Command("exportstats", "экспортстатистики"))
@router.message(F.text.regexp(r"(?i)^\s*экспорт статистики\s*$"))
async def owner_export_stats(message: Message) -> None:
    if not await _owner_only(message): return
    rows=await asyncio.to_thread(db_rows, "bot_users", limit=1000)
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
    rows=await asyncio.to_thread(db_rows,"bot_users",limit=10,order="messages",chat_id=message.chat.id)
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
    row=await asyncio.to_thread(db_insert, "bot_reports", {"chat_id":message.chat.id,"reporter_id":message.from_user.id,"message_id":None,"details":"ПРЕДЛОЖЕНИЕ: "+text_value[:1400],"status":"suggestion","created_at":datetime.now(timezone.utc).isoformat()})
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
    row=await asyncio.to_thread(db_insert, "bot_notes", {"user_id":message.from_user.id,"content":f"ИЗБРАННОЕ · chat={src.chat.id} · message={src.message_id}\n{text_value}","created_at":datetime.now(timezone.utc).isoformat()})
    await message.answer("⭐ Сообщение сохранено в личные заметки." if row else "Не удалось сохранить: проверь таблицу bot_notes из schema.sql.")

@router.message(Command("achievements", "достижения"))
@router.message(F.text.regexp(r"(?i)^\s*достижения\s*$"))
async def achievements_cmd(message: Message) -> None:
    if not message.from_user:
        return
    row=await asyncio.to_thread(db_one,"bot_users",user_id=message.from_user.id,chat_id=message.chat.id) or {}
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
    rows=await asyncio.to_thread(db_rows,"bot_users",limit=20,order="last_seen",chat_id=message.chat.id)
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
    exc = getattr(event, "exception", event)
    log.exception("Unhandled update error: %r", exc)
    # Never leave a user with complete silence after a handler crashes.
    update = getattr(event, "update", None)
    message = (
        getattr(update, "message", None)
        or getattr(update, "edited_message", None)
        or getattr(getattr(update, "callback_query", None), "message", None)
    )
    if message is not None:
        try:
            await message.answer(
                "⚠️ Команда вызвала внутреннюю ошибку. Ошибка записана в журнал; "
                "повтори команду позже или сообщи владельцу бота."
            )
        except Exception:
            log.exception("Could not send the user-facing internal-error notice")
    return True

# Extra Russian moderation aliases. Normalize synonyms before invoking the existing
# permission-checked handlers so reply-to-message targeting and protections remain.
_MODERATION_ALIAS_GROUPS = [
    (re.compile(r"(?i)^\s*[.!/]?(?:выкинуть|выкинь|выкиньте|вышвырнуть|вышвырни|вышвырните|выдворить|выдворите|удалить\s+из\s+чата|убрать\s+из\s+чата)\b\s*(.*)$"), "кик"),
    (re.compile(r"(?i)^\s*[.!/]?(?:заблокировать|заблокируй|заблокируйте|забанить|забань|забаньте|вбанить|вбань|бань|баньте|отправить\s+в\s+бан|дать\s+бан|выдать\s+бан)\b\s*(.*)$"), "бан"),
    (re.compile(r"(?i)^\s*[.!/]?(?:отбанить|разбанить|разбань|разбаньте|разблокировать|разблокируй|разблокируйте|снять\s+блокировку|отменить\s+бан|отмени\s+бан|отмените\s+бан)\b\s*(.*)$"), "разбан"),
    (re.compile(r"(?i)^\s*[.!/]?(?:замутить|замуть|замутьте|заглушить|заглуши|заглушите|приглушить|приглуши|приглушите|заткнуть|заткни|заткните|выдать\s+мут|дать\s+мут|включить\s+мут)\b\s*(.*)$"), "мут"),
    (re.compile(r"(?i)^\s*[.!/]?(?:разглушить|разглуши|разглушите|анмутить|анмуть|анмутьте|снять\s+ограничение|вернуть\s+голос|убрать\s+мут|сними\s+мут|снимите\s+мут)\b\s*(.*)$"), "размут"),
    (re.compile(r"(?i)^\s*[.!/]?(?:выдать\s+предупреждение|дать\s+предупреждение|объявить\s+предупреждение|предупреди|предупредите|выписать\s+варн|выпиши\s+варн|выпишите\s+варн|замечание)\b\s*(.*)$"), "варн"),
    (re.compile(r"(?i)^\s*[.!/]?(?:убрать\s+предупреждение|снять\s+предупреждение|сними\s+предупреждение|снимите\s+предупреждение|удалить\s+варн|удали\s+варн|удалите\s+варн|аннулировать\s+варн)\b\s*(.*)$"), "снять варн"),
]
@router.message(F.text.regexp(r"(?i)^\s*[.!/]?(?:выкинуть|выкинь|выкиньте|вышвырнуть|вышвырни|вышвырните|удалить\s+из\s+чата|убрать\s+из\s+чата|заблокировать|заблокируй|заблокируйте|забанить|забань|забаньте|вбанить|вбань|бань|баньте|отбанить|разбанить|разбань|разбаньте|разблокировать|разблокируй|разблокируйте|снять\s+блокировку|отменить\s+бан|отмени\s+бан|отмените\s+бан|замутить|замуть|замутьте|заглушить|заглуши|заглушите|приглушить|приглуши|приглушите|заткнуть|заткни|заткните|выдать\s+мут|дать\s+мут|включить\s+мут|разглушить|разглуши|разглушите|анмутить|анмуть|анмутьте|снять\s+ограничение|вернуть\s+голос|убрать\s+мут|сними\s+мут|снимите\s+мут|выдать\s+предупреждение|дать\s+предупреждение|объявить\s+предупреждение|предупреди|предупредите|выписать\s+варн|выпиши\s+варн|выпишите\s+варн|замечание|убрать\s+предупреждение|снять\s+предупреждение|сними\s+предупреждение|снимите\s+предупреждение|удалить\s+варн|удали\s+варн|удалите\s+варн|аннулировать\s+варн)(?:\s|$)"))
async def extra_moderation_aliases(message: Message) -> None:
    raw = (message.text or "").strip()
    for pattern, canonical in _MODERATION_ALIAS_GROUPS:
        match = pattern.match(raw)
        if not match:
            continue
        remainder = match.group(1).strip()
        synthetic = message.model_copy(update={"text": f"{canonical} {remainder}".strip()})
        if canonical == "кик":
            await kick_cmd(synthetic)
        elif canonical == "бан":
            await ban_cmd(synthetic)
        elif canonical == "разбан":
            await unban_cmd(synthetic)
        elif canonical == "мут":
            await mute_cmd(synthetic)
        elif canonical == "размут":
            await unmute_cmd(synthetic)
        elif canonical == "варн":
            await warn_cmd(synthetic)
        elif canonical == "снять варн":
            await unwarn_cmd(synthetic)
        return


async def main() -> None:
    acquire_single_instance_lock()
    # Меню команд разделено по контекстам: в группах — только управление чатом,
    # обычным пользователям в личке — пользовательские функции, овнерские команды —
    # только в личном чате владельца бота.
    group_commands = [
        BotCommand(command="start", description="Запустить бота"),
        BotCommand(command="help", description="Справка по командам чата"),
        BotCommand(command="commands", description="Все команды Pear CM"),
        BotCommand(command="admins", description="Администраторы чата"),
        BotCommand(command="warns", description="Мои предупреждения"),
        BotCommand(command="top", description="Топ за период"),
        BotCommand(command="mute", description="Мут (ответом)"),
        BotCommand(command="unmute", description="Снять мут (ответом)"),
        BotCommand(command="ban", description="Бан (ID/@username/ответом)"),
        BotCommand(command="unban", description="Разбанить по ID/@username"),
        BotCommand(command="premium", description="Статус Pear Premium"),
        BotCommand(command="kick", description="Исключить (ответом)"),
        BotCommand(command="warn", description="Предупреждение (ответом)"),
        BotCommand(command="del", description="Удалить сообщения"),
        BotCommand(command="openchat", description="Открыть чат"),
        BotCommand(command="closechat", description="Закрыть чат"),
        BotCommand(command="tempban", description="Временный бан"),
        BotCommand(command="history", description="История наказаний"),
        BotCommand(command="note", description="Заметка о пользователе"),
        BotCommand(command="trusted", description="Доверенные участники"),
        BotCommand(command="reasons", description="Шаблоны причин"),
        BotCommand(command="modrights", description="Пороги рангов команд"),
        BotCommand(command="filteraction", description="Действие антиспама"),
        BotCommand(command="schedule", description="Расписание чата"),
        BotCommand(command="modstats", description="Статистика модераторов"),
        BotCommand(command="marriages", description="Все браки чата"),
        BotCommand(command="whois", description="Карточка участника"),
        BotCommand(command="economy", description="Виртуальная экономика"),
        BotCommand(command="clan", description="Кланы чата"),
        BotCommand(command="profileform", description="Анкета участника"),
        BotCommand(command="awards", description="Мои награды"),
        BotCommand(command="sbor", description="Отметить участников чата"),
        BotCommand(command="exportsettings", description="Экспорт настроек"),
        BotCommand(command="importsettings", description="Импорт настроек"),
    ]
    private_commands = [
        BotCommand(command="start", description="Открыть меню бота"),
        BotCommand(command="help", description="Справка"),
        BotCommand(command="commands", description="Все команды Pear CM"),
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
    # Set a versioned Mini App URL so Telegram WebView does not keep showing a stale cached page.
    try:
        await bot.set_chat_menu_button(
            menu_button=MenuButtonWebApp(
                text="Открыть Pear CM",
                web_app=WebAppInfo(url=MINI_APP_URL),
            )
        )
        log.info("Telegram Mini App menu button configured: %s", MINI_APP_URL)
    except Exception:
        log.exception("Не удалось обновить кнопку Mini App в меню Telegram")
    await bot.set_my_commands(group_commands, scope=BotCommandScopeAllGroupChats())
    await bot.set_my_commands(private_commands, scope=BotCommandScopeAllPrivateChats())
    await bot.set_my_commands(private_commands + owner_commands, scope=BotCommandScopeChat(chat_id=OWNER_ID))
    await bot.delete_webhook(drop_pending_updates=False)
    log.info("Bot started")
    activity_writer = asyncio.create_task(_activity_writer_loop(), name="pear-activity-writer")
    schedule_worker = asyncio.create_task(_schedule_worker(), name="pear-chat-scheduler")
    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    except TelegramConflictError:
        log.critical(
            "TelegramConflictError: another process is polling getUpdates for this token. "
            "Stop every other bot instance / workflow using this token before restarting."
        )
        raise SystemExit(23)
    finally:
        # Do not discard queued counters immediately during normal restarts.
        try:
            await _drain_activity_queue(timeout=15.0)
        except Exception:
            log.exception("Could not drain activity queue during shutdown")
        activity_writer.cancel()
        schedule_worker.cancel()
        for task in (activity_writer, schedule_worker):
            try:
                await task
            except asyncio.CancelledError:
                pass
        await bot.session.close()



# ── Iris-style expanded community features ────────────────────────────────────
def _feature_normalize(value: str) -> str:
    value = (value or "").casefold().replace("ё", "е")
    value = re.sub(r"[\u200b-\u200f\uFEFF]", "", value)
    return re.sub(r"\s+", " ", value).strip()

async def _feature_target(message: Message, explicit: str | None = None):
    if message.reply_to_message and message.reply_to_message.from_user:
        u = message.reply_to_message.from_user
        return {"user_id":u.id,"username":u.username,"first_name":u.full_name}
    token = (explicit or "").strip()
    if token.startswith("@"):
        token = token[1:]
        rows = await asyncio.to_thread(db_rows, "bot_users", 1000, "last_seen", chat_id=message.chat.id)
        for row in rows:
            if str(row.get("username") or "").casefold() == token.casefold():
                return row
        for (cid, uid), info in list(_RECENT_CHAT_USERS.items()):
            if cid == message.chat.id and str(info[0] or "").casefold() == token.casefold():
                return {"user_id":uid,"username":info[0],"first_name":info[1]}
        return None
    if token.isdigit():
        uid = int(token)
        row = await asyncio.to_thread(db_one, "bot_users", user_id=uid, chat_id=message.chat.id)
        if row:
            return row
        recent = _RECENT_CHAT_USERS.get((message.chat.id, uid))
        return {"user_id":uid,"username":recent[0] if recent else None,"first_name":recent[1] if recent else f"ID {uid}"}
    return None

def _feature_safe_name(row: dict) -> str:
    name = str(row.get("first_name") or row.get("user1_name") or "Участник")
    return escape(name[:80])

@router.message(Command("whois"))
@router.message(F.text.regexp(r"(?i)^\s*(?:кто я|кто ты|кто он|кто она|мой статус)(?:\s|$)"))
async def who_is_cmd(message: Message) -> None:
    if not message.from_user:
        return
    text_value = (message.text or "").strip()
    text_value = re.sub(r"(?i)^/whois(?:@\w+)?\s*", "", text_value).strip() or "кто я"
    first = _feature_normalize(text_value.split()[0]) if text_value.split() else "кто я"
    target = None
    if first in {"кто я", "мой статус"} or _feature_normalize(text_value) == "кто я":
        target = await asyncio.to_thread(db_one, "bot_users", user_id=message.from_user.id, chat_id=message.chat.id)
        target = target or {"user_id":message.from_user.id,"username":message.from_user.username,"first_name":message.from_user.full_name,"rank":0,"messages":0,"warns":0}
    else:
        rest = re.sub(r"(?i)^\s*(?:кто ты|кто он|кто она)\s*", "", text_value).strip()
        target = await _feature_target(message, rest or None)
    if not target:
        await message.answer("Не нашёл участника. Ответь на его сообщение командой «кто ты» или укажи @username, который уже встречался в этом чате.")
        return
    uid = int(target.get("user_id") or 0)
    user = await asyncio.to_thread(db_one, "bot_users", user_id=uid, chat_id=message.chat.id) or target
    rank = max(0,min(5,int(user.get("rank") or 0)))
    warns = int(user.get("warns") or 0)
    msgs = int(user.get("messages") or 0)
    name = user.get("first_name") or target.get("first_name") or "Участник"
    uname = ("@" + str(user.get("username"))) if user.get("username") else "нет username"
    await message.answer(
        f"🪪 <b>Карточка участника</b>\nИмя: {mention(uid, str(name))}\n"
        f"Username: <code>{escape(uname)}</code>\nID: <code>{uid}</code>\n"
        f"Ранг: <b>{role_name(rank)}</b> ({rank})\nСообщений: <b>{msgs}</b>\nПредупреждений: <b>{warns}</b>"
    )

@router.message(F.text.regexp(r"(?i)^\s*лдк(?:\s|$)"))
async def personal_command_access_cmd(message: Message) -> None:
    if not await require_real_admin(message):
        return
    parts = (message.text or "").strip().split()
    settings = await asyncio.to_thread(get_chat_settings, message.chat.id)
    rules = settings.setdefault("personal_command_access", {})
    if len(parts) == 1 or (len(parts) == 2 and parts[1].lower() in {"список","помощь","help"}):
        lines = ["<b>🔐 Личный доступ к командам</b>",
                 "Формат: <code>лдк мут запрет</code> ответом на сообщение участника",
                 "<code>лдк мут разрешить</code> ответом на сообщение",
                 "<code>лдк мут сброс</code> ответом на сообщение",
                 "Доступные команды: мут, размут, бан, разбан, кик, варн, снять варн, повысить, понизить, очистить."]
        await message.answer("\n".join(lines))
        return
    if len(parts) < 3:
        await message.answer("Формат: <code>лдк команда запрет|разрешить|сброс</code> ответом на сообщение.")
        return
    action = _feature_normalize(parts[-1])
    command_phrase = _feature_normalize(" ".join(parts[1:-1]))
    aliases = {
        "мут":"mute","мутами":"mute","замут":"mute","размут":"unmute","бан":"ban","баны":"ban",
        "разбан":"unban","кик":"kick","варн":"warn","варны":"warn","снять варн":"unwarn",
        "снять варны":"unwarn","повысить":"promote","повышение":"promote","понизить":"demote",
        "понижение":"demote","очистить":"purge","удалить сообщения":"delete","пин":"pin","анпин":"unpin"
    }
    key = aliases.get(command_phrase)
    if not key:
        tokens = command_phrase.split()
        if any(t.startswith(("размут","анмут","унмут","снят","снять","убрат","убрать","отключ")) for t in tokens) and any(t.startswith("мут") for t in tokens):
            key = "unmute"
        elif any(t.startswith(("мут","замут")) for t in tokens):
            key = "mute"
        elif any(t.startswith(("разбан","снят","снять","убрат","убрать","отмен")) for t in tokens) and any(t.startswith("бан") for t in tokens):
            key = "unban"
        elif any(t.startswith(("бан","забан")) for t in tokens):
            key = "ban"
        elif any(t.startswith(("снят","снять","убрат","убрать","удал","анварн")) for t in tokens) and any(t.startswith(("варн","предупрежд")) for t in tokens):
            key = "unwarn"
        elif any(t.startswith(("варн","предупрежд")) for t in tokens):
            key = "warn"
        elif any(t.startswith(("повыс","повыш")) for t in tokens):
            key = "promote"
        elif any(t.startswith(("пониз","пониж")) for t in tokens):
            key = "demote"
    if not key:
        await message.answer("Неизвестная команда. Пример: <code>лдк мут запрет</code> ответом на сообщение.")
        return
    target = await _feature_target(message)
    if not target:
        await message.answer("Для личного доступа обязательно ответь на сообщение участника.")
        return
    uid = str(int(target.get("user_id") or 0))
    if uid == "0" or int(uid) == (message.from_user.id if message.from_user else 0):
        await message.answer("Нельзя назначить правило самому себе или неизвестному участнику.")
        return
    per_command = rules.setdefault(key, {})
    if action in {"сброс","сбросить","reset","удалить"}:
        per_command.pop(uid, None)
        if not per_command:
            rules.pop(key, None)
        result = "Индивидуальное правило удалено."
    elif action in {"запрет","запретить","запрещен","запрещена","deny","off"}:
        per_command[uid] = "deny"
        result = "Команда запрещена этому участнику."
    elif action in {"разрешить","разрешен","разрешена","allow","on"}:
        per_command[uid] = "allow"
        result = "Команда разрешена этому участнику."
    else:
        await message.answer("Последний аргумент: запрет, разрешить или сброс.")
        return
    settings["personal_command_access"] = rules
    save_chat_settings(message.chat.id, settings)
    audit(message.chat.id, message.from_user.id, "personal_command_access", int(uid), f"{key}={per_command.get(uid, 'reset')}")
    await message.answer(f"🔐 {result}\nУчастник: {mention(int(uid), str(target.get('first_name') or 'Участник'))}\nКоманда: <b>{escape(command_phrase)}</b>.")

@router.message(Command("marriages"))
@router.message(F.text.regexp(r"(?i)^\s*(?:браки|список браков|все браки|мой брак|брак участника|чей брак)(?:\s|$)"))
async def marriages_list_cmd(message: Message) -> None:
    raw = _feature_normalize(message.text or "")
    if raw.startswith(("мой брак","чей брак","брак участника")):
        if raw.startswith("мой брак") and message.from_user:
            target = {"user_id":message.from_user.id,"first_name":message.from_user.full_name,"username":message.from_user.username}
        else:
            target = await _feature_target(message, " ".join((message.text or "").split()[2:]) or None)
        if not target:
            await message.answer("Ответь на сообщение участника или укажи его @username: <code>брак участника @username</code>.")
            return
        uid = int(target.get("user_id") or 0)
        rows = await asyncio.to_thread(db_rows, "bot_marriages", 1000, "created_at", chat_id=message.chat.id)
        row = next((r for r in rows if r.get("status") == "married" and uid in (int(r.get("user1_id") or 0), int(r.get("user2_id") or 0))), None)
        if not row:
            await message.answer("💔 У этого участника сейчас нет зарегистрированного брака в этом чате.")
            return
        await message.answer(f"💍 {mention(int(row['user1_id']),str(row.get('user1_name') or 'Участник'))} ❤️ {mention(int(row['user2_id']),str(row.get('user2_name') or 'Участник'))}")
        return
    rows = await asyncio.to_thread(db_rows, "bot_marriages", 1000, "created_at", chat_id=message.chat.id)
    pairs = [r for r in rows if r.get("status") == "married"]
    if not pairs:
        await message.answer("💔 В этом чате пока нет зарегистрированных браков.")
        return
    lines = ["💍 <b>Браки участников чата</b>"]
    for row in pairs[:100]:
        lines.append(f"• {mention(int(row.get('user1_id') or 0), str(row.get('user1_name') or 'Участник'))} ❤️ {mention(int(row.get('user2_id') or 0), str(row.get('user2_name') or 'Участник'))}")
    if len(pairs) > 100:
        lines.append(f"…и ещё {len(pairs)-100} пар.")
    await message.answer("\n".join(lines))

@router.message(Command("awards"))
@router.message(F.text.regexp(r"(?i)^\s*(?:награда|выдать награду|награды)(?:\s|$)"))
async def award_cmd(message: Message) -> None:
    raw = (message.text or "").strip()
    lowered = _feature_normalize(raw)
    if lowered == "/awards" or (lowered.startswith("награды") and len(raw.split()) == 1):
        if not message.from_user:
            return
        rows = await asyncio.to_thread(db_rows, "bot_awards", 100, "created_at", chat_id=message.chat.id, user_id=message.from_user.id)
        await message.answer("<b>🏅 Твои награды</b>\n" + ("\n".join(f"• {escape(str(r.get('award') or 'Награда'))}" for r in rows) if rows else "Пока наград нет."))
        return
    if not await require_real_admin(message):
        return
    target = await _feature_target(message)
    parts = raw.split(maxsplit=1)
    award_text = parts[1] if len(parts) > 1 else ""
    if message.reply_to_message:
        # A reply makes the whole remaining text after the command the award title.
        award_text = re.sub(r"(?i)^\s*(?:выдать\s+)?награду\s*", "", raw).strip()
    if not target or not award_text or _feature_normalize(award_text) in {"@"+str(target.get("username") or "").lower(), str(target.get("user_id") or "")}:
        await message.answer("Ответь на сообщение участника: <code>награда Помощник чата</code>.")
        return
    row = await asyncio.to_thread(db_insert, "bot_awards", {
        "chat_id":message.chat.id,"user_id":int(target["user_id"]),"award":award_text[:120],
        "actor_id":message.from_user.id,"created_at":datetime.now(timezone.utc).isoformat()
    })
    await message.answer("🏅 Награда вручена!" if row else "Не удалось сохранить награду. Проверь таблицу bot_awards в schema.sql.")

@router.message(Command("profileform"))
@router.message(F.text.regexp(r"(?i)^\s*анкета(?:\s|$)"))
async def member_questionnaire_cmd(message: Message) -> None:
    if not message.from_user:
        return
    raw = (message.text or "").strip()
    rest = re.sub(r"(?i)^\s*(?:анкета|/profileform(?:@\w+)?)\s*", "", raw).strip()
    target = await _feature_target(message) if message.reply_to_message else {"user_id":message.from_user.id,"first_name":message.from_user.full_name,"username":message.from_user.username}
    uid = int(target.get("user_id") or 0) if target else 0
    if not uid:
        await message.answer("Не удалось определить участника.")
        return
    if rest and uid != message.from_user.id:
        await message.answer("Редактировать можно только собственную анкету.")
        return
    row = await asyncio.to_thread(db_one, "bot_member_profiles", user_id=uid, chat_id=message.chat.id) or {}
    if not rest:
        name = row.get("display_name") or target.get("first_name") or "Участник"
        await message.answer(
            f"🪪 <b>Анкета участника</b>\nИмя: {escape(str(name))}\n"
            f"Интересы: {escape(str(row.get('interests') or 'не указаны'))}\n"
            f"О себе: {escape(str(row.get('about') or 'пока пусто'))}\n\n"
            "Заполнить: <code>анкета интересы музыка, игры</code>\n"
            "О себе: <code>анкета о себе люблю общаться</code>\n"
            "Очистить: <code>анкета очистить</code>"
        )
        return
    patch = {"chat_id":message.chat.id,"user_id":uid,"display_name":message.from_user.full_name,
             "updated_at":datetime.now(timezone.utc).isoformat()}
    if rest.lower() in {"очистить","сброс","удалить"}:
        patch.update({"interests":"","about":""})
    elif _feature_normalize(rest).startswith("интересы "):
        patch["interests"] = rest.split(maxsplit=1)[1][:300]
        if row.get("about") is not None: patch["about"] = row.get("about")
    elif _feature_normalize(rest).startswith(("о себе ","обо мне ")):
        patch["about"] = rest.split(maxsplit=2)[-1][:500]
        if row.get("interests") is not None: patch["interests"] = row.get("interests")
    else:
        await message.answer("Формат: <code>анкета интересы ...</code>, <code>анкета о себе ...</code> или <code>анкета очистить</code>.")
        return
    saved = await asyncio.to_thread(db_upsert, "bot_member_profiles", patch, "chat_id,user_id")
    await message.answer("✅ Анкета обновлена." if saved else "Не удалось сохранить анкету. Проверь таблицу bot_member_profiles в schema.sql.")

# Role-play commands; they also build a lightweight relationship score between the two users.
_RP_ACTIONS = {
    "обнять":("🤗","обнял(а)"), "обнимашки":("🤗","обнял(а)"), "поцеловать":("😘","поцеловал(а)"),
    "чмокнуть":("😘","чмокнул(а)"), "обнять крепко":("🫂","крепко обнял(а)"),
    "дать пять":("🖐️","дал(а) пять"), "пожать руку":("🤝","пожал(а) руку"),
    "погладить":("🐾","погладил(а)"), "похвалить":("✨","похвалил(а)"), "поздравить":("🎉","поздравил(а)"),
    "поддержать":("💚","поддержал(а)"), "утешить":("🫶","утешил(а)"), "успокоить":("🫂","успокоил(а)"),
    "пнуть":("🦶","пнул(а)"), "ударить":("💥","ударил(а)"), "укусить":("🦷","укусил(а)"),
    "ущипнуть":("🤏","ущипнул(а)"), "пощекотать":("🤣","пощекотал(а)"), "покормить":("🍓","покормил(а)"),
    "угостить":("🍰","угостил(а)"), "пригласить на свидание":("🌹","пригласил(а) на свидание"),
    "подарить":("🎁","подарил(а)"), "пожелать удачи":("🍀","пожелал(а) удачи"),
    "помочь":("🫶","помог(ла)"), "спасти":("🦸","спас(ла)"), "обидеть":("💔","обидел(а)"),
    "простить":("🤍","простил(а)"), "помириться":("🕊️","помирил(а)ся"), "поблагодарить":("🙏","поблагодарил(а)"),
    "поклониться":("🙇","поклонил(а)ся"), "пожелать спокойной ночи":("🌙","пожелал(а) спокойной ночи"),
    "отношения":("❤️","посмотрел(а) отношения")
}
_RP_ACTION_PATTERN = "|".join(re.escape(x) for x in sorted(_RP_ACTIONS, key=len, reverse=True))
@router.message(F.text.regexp(r"(?i)^\s*(?:обнять|обнимашки|поцеловать|чмокнуть|обнять крепко|дать пять|пожать руку|погладить|похвалить|поздравить|поддержать|утешить|успокоить|пнуть|ударить|укусить|ущипнуть|пощекотать|покормить|угостить|пригласить на свидание|подарить|пожелать удачи|помочь|спасти|обидеть|простить|помириться|поблагодарить|поклониться|пожелать спокойной ночи|отношения)(?:\s|$)"))
async def roleplay_relationship_cmd(message: Message) -> None:
    if not message.from_user:
        return
    raw = _feature_normalize(message.text or "")
    if raw in {"отн действия", "отношения действия", "действия отношений", "список действий"}:
        actions = [
            "обнять", "поцеловать", "чмокнуть", "обнять крепко", "дать пять", "пожать руку", "погладить",
            "похвалить", "поздравить", "поддержать", "утешить", "успокоить", "пнуть", "ударить", "укусить",
            "ущипнуть", "пощекотать", "покормить", "угостить", "пригласить на свидание", "подарить айфон",
            "подарить цветы", "подарить шоколад", "пожелать удачи", "помочь", "спасти", "обидеть", "простить",
            "помириться", "поблагодарить", "поклониться", "пожелать спокойной ночи", "отношения"
        ]
        await message.answer("💞 <b>Действия отношений</b>\n" + "\n".join(f"• <code>{escape(x)}</code>" for x in actions) + "\n\nПрименение: ответь на сообщение участника нужной командой. Подарки: <code>подарить айфон</code>, <code>подарить цветы</code> и т. п.")
        return
    action = next((x for x in sorted(_RP_ACTIONS,key=len,reverse=True) if raw == x or raw.startswith(x+" ")), None)
    gift_name = raw[len("подарить"):].strip() if raw.startswith("подарить ") else ""
    if raw.startswith("отношения"):
        target = await _feature_target(message, raw[len("отношения"):].strip() or None)
        if not target:
            await message.answer("Ответь на сообщение участника командой «отношения».")
            return
        a,b = sorted((message.from_user.id,int(target.get("user_id") or 0)))
        row = await asyncio.to_thread(db_one, "bot_relationships", chat_id=message.chat.id, user1_id=a, user2_id=b) or {}
        own_name = message.from_user.full_name or "Участник"
        target_name = str(target.get("first_name") or "Участник")
        a_name = own_name if a == message.from_user.id else target_name
        b_name = target_name if b == int(target.get("user_id") or 0) else own_name
        await message.answer(f"❤️ <b>Отношения</b>\n{mention(a,a_name)} и {mention(b,b_name)}\nОчки связи: <b>{int(row.get('points') or 0)}</b>")
        return
    if action == "подарить" and not gift_name:
        await message.answer("Напиши, что подарить, например «подарить айфон», ответом на сообщение участника.")
        return
    target_arg = gift_name if action == "подарить" else (raw[len(action):].strip() if action else None)
    target = await _feature_target(message, None if action == "подарить" else target_arg)
    if not target:
        await message.answer("Ответь на сообщение участника или укажи его @username.")
        return
    uid = int(target.get("user_id") or 0)
    if uid == message.from_user.id or uid <= 0:
        await message.answer("Эту команду нужно применить к другому участнику.")
        return
    emoji, verb = _RP_ACTIONS[action]
    actor = message.from_user.full_name or "Участник"
    partner = target.get("first_name") or "Участник"
    a,b = sorted((message.from_user.id,uid))
    existing = await asyncio.to_thread(db_one, "bot_relationships", chat_id=message.chat.id, user1_id=a, user2_id=b) or {}
    points = int(existing.get("points") or 0) + (4500 if action == "подарить" and gift_name == "айфон" else 1)
    saved = await asyncio.to_thread(db_upsert, "bot_relationships", {
        "chat_id":message.chat.id,"user1_id":a,"user2_id":b,"points":points,
        "updated_at":datetime.now(timezone.utc).isoformat()
    }, "chat_id,user1_id,user2_id")
    if action == "подарить":
        item_emoji = {"айфон":"📱","цветы":"💐","шоколад":"🍫","кольцо":"💍","машину":"🚗","машина":"🚗","плюшевого мишку":"🧸"}.get(gift_name, "🎁")
        await message.answer(f"💓 {item_emoji} | {mention(message.from_user.id,actor)} подарил(а) <b>{escape(gift_name)}</b> {mention(uid,str(partner))}!\n🔅 Крепость отношений выросла на {4500 if gift_name == 'айфон' else 1} hp.")
    else:
        await message.answer(f"{emoji} {mention(message.from_user.id,actor)} {verb} {mention(uid,str(partner))}!\n❤️ Очки отношений: <b>{points}</b>")

# Virtual economy
@router.message(Command("economy"))
@router.message(F.text.regexp(r"(?i)^\s*(?:баланс|кошелек|кошелёк|ежедневный бонус|бонус|передать|перевести|магазин|купить)(?:\s|$)"))
async def economy_cmd(message: Message) -> None:
    if not message.from_user:
        return
    raw = (message.text or "").strip()
    norm = _feature_normalize(raw)
    if norm == "/economy":
        norm = "баланс"
    uid = message.from_user.id
    current = await asyncio.to_thread(db_one, "bot_economy", chat_id=message.chat.id, user_id=uid) or {
        "chat_id":message.chat.id,"user_id":uid,"balance":0
    }
    if norm in {"баланс","кошелек","кошелёк"}:
        await message.answer(f"🪙 Баланс: <b>{int(current.get('balance') or 0)}</b> Pear Coins.")
        return
    if norm in {"бонус","ежедневный бонус"}:
        last = current.get("last_bonus")
        if last:
            try:
                elapsed = datetime.now(timezone.utc)-datetime.fromisoformat(str(last).replace("Z","+00:00"))
                if elapsed < timedelta(hours=20):
                    wait = timedelta(hours=20)-elapsed
                    await message.answer(f"⏳ Бонус уже получен. Попробуй через {int(wait.total_seconds()//3600)} ч {int((wait.total_seconds()%3600)//60)} мин.")
                    return
            except Exception:
                pass
        balance = int(current.get("balance") or 0)+100
        saved = await asyncio.to_thread(db_upsert, "bot_economy", {
            **current,"chat_id":message.chat.id,"user_id":uid,"balance":balance,
            "last_bonus":datetime.now(timezone.utc).isoformat(),"updated_at":datetime.now(timezone.utc).isoformat()
        }, "chat_id,user_id")
        await message.answer(f"🎁 Ежедневный бонус: +100 Pear Coins!\n🪙 Баланс: <b>{balance}</b>" if saved else "Не удалось сохранить бонус. Проверь таблицу bot_economy в schema.sql.")
        return
    if norm == "магазин":
        await message.answer("<b>🛍 Магазин Pear Coins</b>\n• Титул «Активный» — 250 монет\n• Титул «Легенда» — 1000 монет\nПокупка: <code>купить активный</code> или <code>купить легенда</code>.")
        return
    if norm.startswith("купить "):
        item = norm.split(maxsplit=1)[1]
        prices = {"активный":250,"титул активный":250,"легенда":1000,"титул легенда":1000}
        price = prices.get(item)
        if price is None:
            await message.answer("В магазине доступны: активный (250) и легенда (1000).")
            return
        balance = int(current.get("balance") or 0)
        if balance < price:
            await message.answer(f"Не хватает монет: нужно {price}, у тебя {balance}.")
            return
        saved = await asyncio.to_thread(db_upsert, "bot_economy", {
            **current,"chat_id":message.chat.id,"user_id":uid,"balance":balance-price,
            "title":item,"updated_at":datetime.now(timezone.utc).isoformat()
        }, "chat_id,user_id")
        await message.answer("🛍 Покупка оформлена!" if saved else "Не удалось оформить покупку.")
        return
    if norm.startswith(("передать ","перевести ")):
        m = re.search(r"(?i)^(?:передать|перевести)\s+(?:(.+?)\s+)?(\d+)\s*$", raw)
        target = await _feature_target(message, (m.group(1) or "").strip() or None) if m else None
        amount = int(m.group(2)) if m else 0
        if not target:
            await message.answer("Ответь на сообщение получателя: <code>передать 50</code> или укажи username.")
            return
        to_uid = int(target.get("user_id") or 0)
        if to_uid == uid or amount <= 0 or amount > 1000000:
            await message.answer("Укажи другого участника и сумму от 1 до 1 000 000.")
            return
        receiver = await asyncio.to_thread(db_one, "bot_economy", chat_id=message.chat.id, user_id=to_uid) or {"chat_id":message.chat.id,"user_id":to_uid,"balance":0}
        sender_balance = int(current.get("balance") or 0)
        if sender_balance < amount:
            await message.answer("Недостаточно монет.")
            return
        now = datetime.now(timezone.utc).isoformat()
        sender_saved = await asyncio.to_thread(db_upsert, "bot_economy", {**current,"chat_id":message.chat.id,"user_id":uid,"balance":sender_balance-amount,"updated_at":now}, "chat_id,user_id")
        receiver_saved = await asyncio.to_thread(db_upsert, "bot_economy", {**receiver,"chat_id":message.chat.id,"user_id":to_uid,"balance":int(receiver.get("balance") or 0)+amount,"updated_at":now}, "chat_id,user_id")
        if not sender_saved or not receiver_saved:
            await message.answer("⚠️ Перевод выполнен не полностью. Нужна проверка базы; не повторяй операцию сразу.")
            return
        await message.answer(f"🪙 Переведено <b>{amount}</b> монет участнику {mention(to_uid,str(target.get('first_name') or 'Участник'))}.")
        return
    await message.answer("Команды экономики: баланс, ежедневный бонус, передать, магазин, купить.")

# Clans
@router.message(Command("clan"))
@router.message(F.text.regexp(r"(?i)^\s*клан(?:\s|$)"))
async def clan_cmd(message: Message) -> None:
    if not message.from_user:
        return
    raw = (message.text or "").strip()
    parts = raw.split(maxsplit=2)
    action = _feature_normalize(parts[1]) if len(parts)>1 else "инфо"
    name = parts[2].strip()[:40] if len(parts)>2 else ""
    uid, chat_id = message.from_user.id, message.chat.id
    if action in {"создать","создай"}:
        if not name or len(name)<2:
            await message.answer("Название клана: <code>клан создать Стражи</code>.")
            return
        current = await asyncio.to_thread(db_one, "bot_clan_members", chat_id=chat_id, user_id=uid)
        if current:
            await message.answer("Ты уже состоишь в клане. Сначала выйди: <code>клан выйти</code>.")
            return
        saved = await asyncio.to_thread(db_insert, "bot_clans", {"chat_id":chat_id,"name":name,"leader_id":uid,"created_at":datetime.now(timezone.utc).isoformat()})
        if not saved:
            await message.answer("Не удалось создать клан. Возможно, название уже занято или таблица bot_clans не создана.")
            return
        joined = await asyncio.to_thread(db_upsert, "bot_clan_members", {"chat_id":chat_id,"user_id":uid,"clan_name":name,"role":"leader","joined_at":datetime.now(timezone.utc).isoformat()}, "chat_id,user_id")
        await message.answer(f"🏰 Клан <b>{escape(name)}</b> создан! Ты его лидер." if joined else "Клан создан, но не удалось записать лидера в состав.")
        return
    if action in {"вступить","вступитьв","join"}:
        if not name:
            await message.answer("Формат: <code>клан вступить Название</code>.")
            return
        existing = await asyncio.to_thread(db_one, "bot_clan_members", chat_id=chat_id, user_id=uid)
        clan = await asyncio.to_thread(db_one, "bot_clans", chat_id=chat_id, name=name)
        if existing:
            await message.answer("Ты уже в клане. Сначала выйди из него.")
            return
        if not clan:
            await message.answer("Клан не найден.")
            return
        saved = await asyncio.to_thread(db_upsert, "bot_clan_members", {"chat_id":chat_id,"user_id":uid,"clan_name":name,"role":"member","joined_at":datetime.now(timezone.utc).isoformat()}, "chat_id,user_id")
        await message.answer(f"🏰 Ты вступил в клан <b>{escape(name)}</b>." if saved else "Не удалось вступить в клан.")
        return
    if action in {"распустить","удалить"}:
        member = await asyncio.to_thread(db_one, "bot_clan_members", chat_id=chat_id, user_id=uid)
        if not member or member.get("role") != "leader":
            await message.answer("Распустить клан может только его лидер.")
            return
        clan_name = str(member.get("clan_name") or "")
        try:
            await asyncio.to_thread(lambda: db.table("bot_clan_members").delete().eq("chat_id",chat_id).eq("clan_name",clan_name).execute())
            await asyncio.to_thread(lambda: db.table("bot_clans").delete().eq("chat_id",chat_id).eq("name",clan_name).execute())
            await message.answer(f"🏚 Клан <b>{escape(clan_name)}</b> распущен.")
        except Exception:
            log.exception("Could not dissolve clan")
            await message.answer("Не удалось распустить клан.")
        return
    if action in {"передать","передать лидерство","лидер"}:
        member = await asyncio.to_thread(db_one, "bot_clan_members", chat_id=chat_id, user_id=uid)
        target = await _feature_target(message)
        if not member or member.get("role") != "leader":
            await message.answer("Передать лидерство может только лидер клана.")
            return
        if not target:
            await message.answer("Ответь на сообщение нового лидера командой <code>клан передать</code>.")
            return
        new_uid = int(target.get("user_id") or 0)
        if new_uid == uid:
            await message.answer("Ты уже лидер этого клана.")
            return
        new_member = await asyncio.to_thread(db_one, "bot_clan_members", chat_id=chat_id, user_id=new_uid)
        if not new_member or new_member.get("clan_name") != member.get("clan_name"):
            await message.answer("Новый лидер должен состоять в твоём клане.")
            return
        clan_name = str(member.get("clan_name") or "")
        now = datetime.now(timezone.utc).isoformat()
        ok1 = await asyncio.to_thread(db_upsert, "bot_clan_members", {**member,"role":"member","joined_at":member.get("joined_at") or now}, "chat_id,user_id")
        ok2 = await asyncio.to_thread(db_upsert, "bot_clan_members", {**new_member,"role":"leader","joined_at":new_member.get("joined_at") or now}, "chat_id,user_id")
        ok3 = await asyncio.to_thread(lambda: db.table("bot_clans").update({"leader_id":new_uid}).eq("chat_id",chat_id).eq("name",clan_name).execute() if db is not None else None)
        await message.answer("👑 Лидерство передано." if ok1 and ok2 and ok3 else "Не удалось полностью передать лидерство. Проверь записи клана.")
        return
    if action in {"выйти","покинуть","leave"}:
        member = await asyncio.to_thread(db_one, "bot_clan_members", chat_id=chat_id, user_id=uid)
        if not member:
            await message.answer("Ты не состоишь в клане.")
            return
        if member.get("role") == "leader":
            await message.answer("Лидер не может выйти, пока не передаст лидерство или не распустит клан.")
            return
        deleted = await asyncio.to_thread(lambda: db.table("bot_clan_members").delete().eq("chat_id",chat_id).eq("user_id",uid).execute() if db is not None else None)
        await message.answer("Ты вышел из клана." if deleted else "Не удалось выйти из клана.")
        return
    if action in {"участники","состав","инфо","информация"}:
        member = await asyncio.to_thread(db_one, "bot_clan_members", chat_id=chat_id, user_id=uid)
        clan_name = name or (member.get("clan_name") if member else None)
        if not clan_name:
            await message.answer("Ты пока не в клане. Создай его командой <code>клан создать Название</code>.")
            return
        clan = await asyncio.to_thread(db_one, "bot_clans", chat_id=chat_id, name=clan_name)
        if not clan:
            await message.answer("Клан не найден.")
            return
        members = await asyncio.to_thread(db_rows, "bot_clan_members", 100, "joined_at", chat_id=chat_id, clan_name=clan_name)
        lines = [f"🏰 <b>Клан {escape(clan_name)}</b>",f"Участников: <b>{len(members)}</b>"]
        for m in members[:30]:
            u = await asyncio.to_thread(db_one, "bot_users", user_id=int(m.get("user_id") or 0), chat_id=chat_id) or {}
            nm = u.get("first_name") or f"ID {m.get('user_id')}"
            lines.append(f"• {escape(str(nm))}" + (" 👑" if m.get("role")=="leader" else ""))
        await message.answer("\n".join(lines))
        return
    if action in {"помощь","help"}:
        await message.answer("<b>🏰 Команды кланов</b>\n<code>клан создать Название</code>\n<code>клан вступить Название</code>\n<code>клан выйти</code>\n<code>клан распустить</code> (лидер)\n<code>клан передать</code> ответом на участника\n<code>клан инфо</code>\n<code>клан участники Название</code>")
        return
    await message.answer("Команды: <code>клан создать Название</code>, <code>клан вступить Название</code>, <code>клан выйти</code>, <code>клан распустить</code>, <code>клан передать</code>, <code>клан инфо</code>, <code>клан участники</code>, <code>клан помощь</code>.")


@router.message(Command("choose"))
@router.message(F.text.regexp(r"(?i)^\s*(?:выбери участника|случайный участник|рандом участника|рандом участник|жребий|да или нет|шипперинг|шиперинг|шипперим|шиперим|шипперимся|шиперимся|шипперить|шиперить|шиппер|шипер|зашипперить|зашиперить|зашиппери|зашипери|кого шипперим|кого шиперим|рандом шиппер|рандом шипер|рандомная пара|случайная пара|шипперни|шиперни|шипперь|шиперь|сошиппери|сошипери)(?:\s|$)"))
async def random_community_cmd(message: Message) -> None:
    raw = _feature_normalize(message.text or "")
    if raw.startswith("/choose"):
        raw = raw[len("/choose"):].strip() or "выбери участника"
    if raw == "да или нет":
        await message.answer("🎲 " + random.choice(["Да! ✅","Нет! ❌","Возможно 🤔","Спроси позже 🔮"]))
        return
    rows = await asyncio.to_thread(db_rows, "bot_users", 1000, "last_seen", chat_id=message.chat.id)
    unique = {}
    for row in rows:
        try:
            uid = int(row.get("user_id") or 0)
        except (TypeError, ValueError):
            continue
        if uid > 0 and uid != (message.from_user.id if message.from_user else 0):
            unique[uid] = row
    members = list(unique.values())
    shipper_aliases = {
        "шипперинг", "шипперим", "шиперим", "шиперинг", "шипперимся", "шиперимся", "шипперить", "шиперить", "шиппер", "шипер",
        "зашипперить", "зашиперить", "зашиппери", "зашипери", "кого шипперим", "кого шиперим",
        "рандом шиппер", "рандом шипер", "рандомная пара", "случайная пара", "шипперни", "шиперни", "шипперь", "шиперь", "сошиппери", "сошипери",
    }
    if raw in shipper_aliases:
        if len(members) < 2:
            await message.answer("Для шипперинга пока недостаточно участников, которых бот знает.")
            return
        pair = random.sample(members, 2)
        names = [p.get("first_name") or p.get("username") or f"ID {p.get('user_id')}" for p in pair]
        await message.answer(f"💞 <b>РАНДОМ ШИППЕРИМ:</b> {mention(int(pair[0]['user_id']),str(names[0]))}⁬ + {mention(int(pair[1]['user_id']),str(names[1]))}⁬. Любите друг друга и берегите. Мур.")
        return
    if not members:
        await message.answer("Пока некого выбирать: бот ещё не видел участников в этом чате.")
        return
    picked = random.choice(members)
    name = picked.get("first_name") or picked.get("username") or f"ID {picked.get('user_id')}"
    await message.answer(f"🎲 Выбран участник: {mention(int(picked['user_id']),str(name))}")

# "анрег" исключает пользователя из общего сбора в текущем чате.
async def participant_call_unregister(message: Message) -> None:
    if not message.from_user or message.from_user.is_bot or not is_group(message):
        return
    chat_id, user_id = message.chat.id, message.from_user.id
    opted_out: set[tuple[int, int]] = getattr(gather_cmd, "_opted_out", set())
    opted_out.add((chat_id, user_id))
    setattr(gather_cmd, "_opted_out", opted_out)
    persisted = False
    if db is not None:
        try:
            await asyncio.to_thread(lambda: db.table("bot_call_optouts").upsert(
                {"chat_id": chat_id, "user_id": user_id},
                on_conflict="chat_id,user_id",
            ).execute())
            persisted = True
        except Exception:
            log.exception("Could not persist call opt-out chat=%s user=%s", chat_id, user_id)
    await audit_wait(chat_id, user_id, "participant_call_unregistered",
                     details=f"username={message.from_user.username or ''}; persistent={persisted}")
    display = escape(message.from_user.full_name or message.from_user.username or "Участник")
    await message.reply(f"<b>{display}</b>, на период неактивности Вы исключены из всеобщего призыва.")

# Telegram Bot API не предоставляет метод перечисления всех участников группы.
# Команда работает по сохранённым ID; «калл»/«call» — удобные текстовые алиасы.
@router.message(Command("sbor"))
@router.message(F.text.regexp(r"(?i)^\s*/sbor(?:@pear_cm_bot)?(?:\s|$)"))
@router.message(F.text.regexp(r"(?i)^\s*(?:калл|колл|call|сбор|общий сбор|вызов участников)(?:\s|$)"))
async def gather_cmd(message: Message) -> None:
    raw = (message.text or "").strip()
    log.info("Received participant call in chat_id=%s from user_id=%s text=%r", message.chat.id, message.from_user.id if message.from_user else None, raw)
    if not await require_real_admin(message):
        if message.from_user:
            await audit_wait(message.chat.id, message.from_user.id, "participant_call_denied",
                             details=f"text={raw[:300]!r}; reason=not_telegram_admin")
        return

    await audit_wait(message.chat.id, message.from_user.id if message.from_user else 0,
                     "participant_call_started", details=f"text={raw[:300]!r}; chat_title={message.chat.title or ''}")

    # Для «калл проверка», «call проверка» и «/sbor проверка» текст после команды — причина.
    parts = raw.split(maxsplit=1)
    notice = parts[1].strip() if len(parts) > 1 else "📢 Общий сбор!"
    if raw.lower().startswith(("/sbor", "/sbor@")) is False and parts:
        first = parts[0].casefold()
        if first in {"калл", "колл", "call", "сбор"}:
            notice = parts[1].strip() if len(parts) > 1 else "📢 Общий сбор!"
    safe_notice = escape(notice[:500])
    opted_out: set[tuple[int, int]] = getattr(gather_cmd, "_opted_out", set())

    def load_chat_users() -> list[dict]:
        # Merge three sources: normal profiles, durable message history, and users
        # seen in this process. bot_activity_events keeps historical writers even
        # when a profile row was not created or its upsert failed.
        collected: list[dict] = []
        if db is None:
            collected.extend(db_rows("bot_users", 1000, "last_seen", chat_id=message.chat.id))
            try:
                collected.extend(db_rows("bot_activity_events", 1000, "created_at", chat_id=message.chat.id))
            except Exception:
                log.exception("Could not read activity history for participant call chat=%s", message.chat.id)
            return collected

        page_size = 1000
        # Primary profiles.
        offset = 0
        while True:
            page = (
                db.table("bot_users")
                .select("user_id,first_name,username,chat_id")
                .eq("chat_id", message.chat.id)
                .range(offset, offset + page_size - 1)
                .execute()
                .data or []
            )
            collected.extend(page)
            if len(page) < page_size:
                break
            offset += page_size

        # Historical writers. This intentionally scans every page so older users
        # are included even if their bot_users profile is missing.
        offset = 0
        try:
            while True:
                page = (
                    db.table("bot_activity_events")
                    .select("user_id,first_name,username,chat_id")
                    .eq("chat_id", message.chat.id)
                    .range(offset, offset + page_size - 1)
                    .execute()
                    .data or []
                )
                collected.extend(page)
                if len(page) < page_size:
                    break
                offset += page_size
        except Exception:
            # Keep the main command useful if the installation is missing the
            # activity table; the profiles and in-memory cache still work.
            log.exception("Could not read bot_activity_events for chat=%s", message.chat.id)

        # Include message authors seen since this bot process started, even if
        # persistence is currently delayed or temporarily unavailable.
        for (chat_id, user_id), (username, first_name, _stamp) in list(_RECENT_CHAT_USERS.items()):
            if chat_id == message.chat.id:
                collected.append({
                    "chat_id": chat_id, "user_id": user_id,
                    "username": username, "first_name": first_name,
                })
        return collected
    try:
        rows = await asyncio.to_thread(load_chat_users)
    except Exception as exc:
        log.exception("Failed to load users for participant call in chat %s", message.chat.id)
        await audit_wait(message.chat.id, message.from_user.id if message.from_user else 0,
                         "participant_call_error", details=f"stage=load_users; error={type(exc).__name__}: {str(exc)[:1500]}")
        await message.answer(
            "⚠️ Общий сбор не запущен: ошибка загрузки базы. "
            f"Причина: <code>{escape(type(exc).__name__ + ': ' + str(exc)[:350])}</code>. "
            "Подробности сохранены в логах владельца."
        )
        return

    excluded: set[int] = {uid for cid, uid in opted_out if cid == message.chat.id}
    if db is not None:
        try:
            opt_rows = await asyncio.to_thread(lambda: db.table("bot_call_optouts").select("user_id").eq("chat_id", message.chat.id).execute().data or [])
            excluded.update(int(row["user_id"]) for row in opt_rows if row.get("user_id"))
        except Exception:
            log.exception("Could not load persistent call opt-outs for chat=%s", message.chat.id)
    seen: set[int] = set()
    mentions: list[str] = []
    for row in rows:
        try:
            uid = int(row.get("user_id") or 0)
        except (TypeError, ValueError):
            continue
        if uid <= 0 or uid in seen or uid in excluded:
            continue
        seen.add(uid)
        # Some Telegram display names contain only invisible Unicode characters.
        # Clean them so the call list never shows huge blank-looking names.
        raw_name = str(row.get("first_name") or "").strip()
        name = re.sub(r"[\u200b-\u200f\u202a-\u202e\u2060\ufeff]", "", raw_name).strip()
        if not name:
            name = str(row.get("username") or "").strip().lstrip("@")
        if not name:
            name = "Участник"
        mentions.append(mention(uid, name[:64]))

    if not mentions:
        await audit_wait(message.chat.id, message.from_user.id if message.from_user else 0,
                         "participant_call_error", details="stage=resolve_users; reason=no_saved_ids; check bot_users, bot_activity_events, tracking and Supabase schema")
        await message.answer(
            "⚠️ Общий сбор не запущен: не найдено ни одного сохранённого ID. "
            "Проверь в логах владельца причину и наличие таблиц bot_users / bot_activity_events."
        )
        return

    await audit_wait(message.chat.id, message.from_user.id if message.from_user else 0,
                     "participant_call_resolved", details=f"known_users={len(mentions)}; source_rows={len(rows)}; notice={notice[:300]!r}")
    try:
        # Compact Iris-inspired call card; keep it as one message when possible.
        # Iris-inspired compact card: strong title, neat separators and readable participant list.
        def build_heading(part: int = 1, total: int = 1) -> str:
            part_label = f" <i>· часть {part}/{total}</i>" if total > 1 else ""
            return (
                f"📣 <b>ОБЩИЙ СБОР</b>{part_label}\n"
                f"╭━━━━━━━━━━━━━━━━━━╮\n"
                f"│  🔔 <b>{safe_notice}</b>\n"
                f"╰━━━━━━━━━━━━━━━━━━╯\n"
                f"👥 <b>Кого зовём:</b> {len(mentions)}\n\n"
            )

        heading = build_heading()
        messages: list[str] = []
        body = ""
        for item in mentions:
            separator = "  •  " if body else ""
            if len(heading) + len(body) + len(separator) + len(item) > 3800 and body:
                messages.append(heading + body)
                body = ""
                separator = ""
            body += separator + item
        if body:
            messages.append(heading + body)
        if len(messages) > 1:
            chunks: list[str] = []
            current = ""
            for item in mentions:
                separator = "  •  " if current else ""
                if len(build_heading(99, 99)) + len(current) + len(separator) + len(item) > 3800 and current:
                    chunks.append(current)
                    current = ""
                    separator = ""
                current += separator + item
            if current:
                chunks.append(current)
            total = len(chunks)
            messages = [build_heading(i + 1, total) + chunk for i, chunk in enumerate(chunks)]
        for outgoing in messages:
            await message.answer(outgoing)
        await audit_wait(message.chat.id, message.from_user.id if message.from_user else 0,
                         "participant_call_success", details=f"mentioned={len(mentions)}; messages={len(messages)}; excluded={len(excluded)}; reason={notice[:300]!r}")
    except Exception as exc:
        log.exception("Failed to send participant-call mentions chat=%s count=%s", message.chat.id, len(mentions))
        await audit_wait(message.chat.id, message.from_user.id if message.from_user else 0,
                         "participant_call_error", details=f"stage=send_mentions; sent_before_error; users={len(mentions)}; error={type(exc).__name__}: {str(exc)[:1500]}")
        try:
            await message.answer(f"⚠️ Сбор остановлен при отправке отметок. Причина: <code>{escape(type(exc).__name__ + ': ' + str(exc)[:350])}</code>. Подробности записаны в логи владельца.")
        except Exception:
            log.exception("Could not send participant-call failure notice")


if __name__ == "__main__":
    asyncio.run(main())