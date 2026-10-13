import asyncio
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from html import escape

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatType, ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.types import (
    Message, CallbackQuery, ReplyKeyboardMarkup, KeyboardButton,
    InlineKeyboardMarkup, InlineKeyboardButton, ChatPermissions
)
from supabase import create_client

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("community_bot")

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN") or os.getenv("BOT_TOKEN")
SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_KEY") or os.getenv("SUPABASE_ANON", "")
OWNER_ID = int(os.getenv("OWNER_ID", "6652898792"))
if not TOKEN:
    raise RuntimeError("Добавьте TELEGRAM_BOT_TOKEN в GitHub Secrets.")

db = create_client(SUPABASE_URL, SUPABASE_KEY) if SUPABASE_URL and SUPABASE_KEY and SUPABASE_KEY != "YOUR_KEY" else None
bot = Bot(TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()

# Ранги сообщества (не Telegram-права). Telegram-админов бот не может назначать сам.
RANKS = ["Участник", "Проверенный", "Помощник", "Модератор", "Старший модератор", "Администратор"]
# Минимальный ранг для внутренних команд: 0 участник, 1 проверенный, 2 помощник, 3 модератор...
mem_users = {}
mem_marriages = {}
mem_settings = {}
mem_audit = {}

def kb():
    return ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="📋 Меню"), KeyboardButton(text="👤 Профиль")],
        [KeyboardButton(text="⚠️ Варны"), KeyboardButton(text="🛡️ Права")],
        [KeyboardButton(text="📊 Топ"), KeyboardButton(text="💍 Брак")],
        [KeyboardButton(text="❓ Помощь")],
    ], resize_keyboard=True)

def inline_menu():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👤 Профиль", callback_data="profile"),
         InlineKeyboardButton(text="⚠️ Варны", callback_data="warns")],
        [InlineKeyboardButton(text="🛡️ Права", callback_data="perms"),
         InlineKeyboardButton(text="📊 Топ", callback_data="top")],
        [InlineKeyboardButton(text="💍 Брак", callback_data="marriage"),
         InlineKeyboardButton(text="❓ Помощь", callback_data="help")],
    ])

def user_record(uid, cid, username=None, first_name=None):
    default = {"user_id": uid, "chat_id": cid, "username": username,
               "first_name": first_name or "Пользователь", "warns": 0,
               "rank": 0, "messages": 0, "created_at": datetime.now(timezone.utc).isoformat()}
    if db:
        try:
            r = db.table("bot_users").select("*").eq("user_id", uid).eq("chat_id", cid).limit(1).execute()
            if r.data:
                row = {**default, **r.data[0]}
                return row
            db.table("bot_users").upsert(default, on_conflict="user_id,chat_id").execute()
        except Exception:
            log.exception("Supabase user read/write failed; using temporary fallback")
    return {**default, **mem_users.get((uid, cid), {})}

def save_user(row):
    mem_users[(int(row["user_id"]), int(row["chat_id"]))] = dict(row)
    if db:
        try:
            db.table("bot_users").upsert(row, on_conflict="user_id,chat_id").execute()
        except Exception:
            log.exception("Supabase user save failed")

def audit(cid, actor, action, target=None, details=""):
    event = {"chat_id": cid, "actor_id": actor, "action": action,
             "target_id": target, "details": details[:900],
             "created_at": datetime.now(timezone.utc).isoformat()}
    mem_audit.setdefault(cid, []).append(event)
    if db:
        try:
            db.table("bot_audit").insert(event).execute()
        except Exception:
            log.exception("Supabase audit write failed")

async def is_group_admin(m: Message):
    if not m.from_user:
        return False
    if m.from_user.id == OWNER_ID:
        return True
    if m.chat.type == ChatType.PRIVATE:
        return False
    try:
        member = await bot.get_chat_member(m.chat.id, m.from_user.id)
        return member.status in ("administrator", "creator")
    except (TelegramBadRequest, TelegramForbiddenError):
        return False

async def allowed(m: Message, minimum=3):
    if not m.from_user:
        return False
    if m.from_user.id == OWNER_ID or await is_group_admin(m):
        return True
    row = user_record(m.from_user.id, m.chat.id, m.from_user.username, m.from_user.first_name)
    return int(row.get("rank", 0)) >= minimum

async def get_target(m: Message, args=""):
    if m.reply_to_message and m.reply_to_message.from_user:
        return m.reply_to_message.from_user, args.strip()
    match = re.search(r"@([A-Za-z0-9_]{5,})", args)
    if match:
        try:
            chat = await bot.get_chat("@" + match.group(1))
            return chat, (args[:match.start()] + args[match.end():]).strip()
        except Exception:
            return None, args
    match = re.search(r"(?<!\d)(\d{5,})(?!\d)", args)
    if match:
        try:
            chat = await bot.get_chat(int(match.group(1)))
            return chat, (args[:match.start()] + args[match.end():]).strip()
        except Exception:
            return None, args
    return None, args

def parse_duration(s):
    m = re.fullmatch(r"\s*(\d+)\s*(с|сек|секунд|м|мин|минут|ч|час|часа|часов|д|день|дня|дней|н|нед|неделя|недели|недель)\s*", s.lower())
    if not m:
        return None
    n, unit = int(m.group(1)), m.group(2)
    scale = {"с":1,"сек":1,"секунд":1,"м":60,"мин":60,"минут":60,
             "ч":3600,"час":3600,"часа":3600,"часов":3600,
             "д":86400,"день":86400,"дня":86400,"дней":86400,
             "н":604800,"нед":604800,"неделя":604800,"недели":604800,"недель":604800}
    if n <= 0 or n * scale[unit] > 366 * 86400:
        return None
    return timedelta(seconds=n * scale[unit])

def mention(uid, name="Пользователь"):
    return f'<a href="tg://user?id={uid}">{escape(name or "Пользователь")}</a>'

HELP = """<b>📚 Команды бота</b>

<b>Профиль и сообщество</b>
• <code>профиль</code>, <code>варны</code>, <code>права</code>, <code>топ</code>
• <code>кто админ</code>, <code>кто модер</code>, <code>инфо</code>
• <code>брак</code>, <code>развод</code> — ответом на сообщение или с @username

<b>Модерация</b> (ответом на сообщение или @username)
• <code>мут @user 10м причина</code>, <code>размут @user</code>
• <code>бан @user причина</code>, <code>разбан 123456</code>, <code>кик @user</code>
• <code>варн @user причина</code>, <code>снять варн @user</code>, <code>варны @user</code>
• <code>-смс</code> — удалить сообщение (ответом)
• <code>.пин</code>, <code>.анпин</code>, <code>очистить 10</code>
• <code>повысить @user</code>, <code>понизить @user</code> — внутренний ранг

<b>Важно:</b> бот должен быть администратором группы с нужными правами.
Внутренние ранги не дают Telegram-админку. Команды работают в пределах Telegram Bot API."""

@dp.message(Command("start"))
async def start(m: Message):
    if m.chat.type == ChatType.PRIVATE:
        await m.answer("Привет! Я бот управления сообществом: модерация, профили, ранги и браки.\nДобавь меня в группу и выдай необходимые права администратора.", reply_markup=kb())
        await m.answer("Главное меню:", reply_markup=inline_menu())
    else:
        await m.answer(f"Бот активирован в чате <b>{escape(m.chat.title or 'группа')}</b>.")

@dp.message(Command("help"))
@dp.message(F.text.in_({"❓ Помощь", "помощь", "команды"}))
async def help_cmd(m: Message):
    await m.answer(HELP)

@dp.message(F.text.in_({"📋 Меню", "меню"}))
async def menu(m: Message):
    await m.answer("Главное меню:", reply_markup=inline_menu())

@dp.callback_query()
async def menu_callback(c: CallbackQuery):
    if c.data == "profile":
        await c.message.answer("Открой профиль командой <code>профиль</code> в группе или нажми «👤 Профиль».")
    elif c.data == "warns":
        await c.message.answer("Используй <code>варны</code> в группе.")
    elif c.data == "perms":
        await c.message.answer("Используй <code>права</code> в группе.")
    elif c.data == "top":
        await c.message.answer("Используй <code>топ</code> в группе.")
    elif c.data == "marriage":
        await c.message.answer("Чтобы предложить брак, ответь на сообщение человека командой <code>брак</code>.")
    elif c.data == "help":
        await c.message.answer(HELP)
    await c.answer()

@dp.message(F.text.in_({"👤 Профиль", "профиль", "мой профиль"}))
async def profile(m: Message):
    if not m.from_user: return
    row = user_record(m.from_user.id, m.chat.id, m.from_user.username, m.from_user.first_name)
    row["messages"] = int(row.get("messages", 0)) + 1
    save_user(row)
    rank = max(0, min(int(row.get("rank", 0)), len(RANKS)-1))
    await m.answer(f"👤 {m.from_user.mention_html()}\n⚠️ Варны: <b>{row.get('warns', 0)}</b>\n🏅 Ранг: <b>{RANKS[rank]}</b>\n💬 Сообщений учтено ботом: <b>{row['messages']}</b>")

@dp.message(F.text.in_({"⚠️ Варны", "варны", "мои варны"}))
async def warns(m: Message):
    if not m.from_user: return
    target, _ = await get_target(m, m.text or "")
    user = target or m.from_user
    row = user_record(user.id, m.chat.id, getattr(user, "username", None), getattr(user, "first_name", "Пользователь"))
    await m.answer(f"⚠️ Варны {mention(user.id, getattr(user, 'first_name', 'Пользователь'))}: <b>{row.get('warns', 0)}</b>")

@dp.message(F.text.in_({"🛡️ Права", "права"}))
async def perms(m: Message):
    if not m.from_user: return
    row = user_record(m.from_user.id, m.chat.id, m.from_user.username, m.from_user.first_name)
    rank = max(0, min(int(row.get("rank", 0)), len(RANKS)-1))
    admin = await is_group_admin(m)
    await m.answer(f"Ваш ранг: <b>{RANKS[rank]}</b>\nTelegram-администратор: <b>{'да' if admin else 'нет'}</b>")

@dp.message(F.text.in_({"📊 Топ", "топ", "топ чата"}))
async def top(m: Message):
    rows = []
    if db:
        try:
            res = db.table("bot_users").select("user_id,first_name,rank,messages,warns").eq("chat_id", m.chat.id).order("messages", desc=True).limit(10).execute()
            rows = res.data or []
        except Exception:
            log.exception("Top query failed")
    if not rows:
        rows = [r for (uid, cid), r in mem_users.items() if cid == m.chat.id]
        rows.sort(key=lambda x: int(x.get("messages", 0)), reverse=True)
        rows = rows[:10]
    if not rows:
        await m.answer("Пока нет статистики. Данные будут собираться после активности пользователей.")
        return
    lines = ["<b>📊 Топ участников по учтённым сообщениям</b>"]
    for i, r in enumerate(rows, 1):
        lines.append(f"{i}. {mention(int(r['user_id']), r.get('first_name', 'Участник'))} — {int(r.get('messages',0))}")
    await m.answer("\n".join(lines))

@dp.message(F.text.regexp(r"(?i)^(брак|жениться|выйти замуж)(\s|$)"))
async def marriage(m: Message):
    if not m.from_user or m.chat.type == ChatType.PRIVATE:
        await m.answer("Команда брака работает в группе. Ответь на сообщение человека командой <code>брак</code>.")
        return
    target, _ = await get_target(m, m.text or "")
    if not target or target.id == m.from_user.id or getattr(target, "is_bot", False):
        await m.answer("Ответь на сообщение человека командой <code>брак</code> или укажи @username.")
        return
    existing = mem_marriages.get(m.chat.id, {})
    if m.from_user.id in existing or target.id in existing:
        await m.answer("Один из участников уже состоит в браке. Сейчас брак хранится в оперативной памяти; настрой Supabase для постоянного хранения.")
        return
    mem_marriages.setdefault(m.chat.id, {})[m.from_user.id] = target.id
    mem_marriages[m.chat.id][target.id] = m.from_user.id
    audit(m.chat.id, m.from_user.id, "marriage", target.id)
    await m.answer(f"💍 {m.from_user.mention_html()} и {mention(target.id, getattr(target, 'first_name', 'Пользователь'))} теперь отмечены как пара!")

@dp.message(F.text.in_({"развод", "развестись"}))
async def divorce(m: Message):
    pair = mem_marriages.setdefault(m.chat.id, {})
    partner = pair.pop(m.from_user.id, None) if m.from_user else None
    if partner:
        pair.pop(partner, None)
        audit(m.chat.id, m.from_user.id, "divorce", partner)
        await m.answer("💔 Брак расторгнут.")
    else:
        await m.answer("Вы не состоите в браке по данным бота.")

@dp.message(F.text.lower().startswith("кто админ"))
async def admins(m: Message):
    if m.chat.type == ChatType.PRIVATE:
        await m.answer("Команда работает в группе.")
        return
    try:
        members = await m.chat.get_administrators()
        lines = ["<b>👑 Администраторы чата</b>"]
        for item in members:
            if not item.user.is_bot:
                label = "Создатель" if item.status == "creator" else "Администратор"
                lines.append(f"• {item.user.mention_html()} — {label}")
        await m.answer("\n".join(lines))
    except Exception:
        await m.answer("Не удалось получить список администраторов.")

@dp.message(F.text.lower().startswith("кто модер"))
async def moderators(m: Message):
    if m.chat.type == ChatType.PRIVATE:
        await m.answer("Команда работает в группе.")
        return
    rows = []
    try:
        for (uid, cid), row in mem_users.items():
            if cid == m.chat.id and int(row.get("rank", 0)) >= 3:
                rows.append(f"• {mention(uid, row.get('first_name'))} — {RANKS[min(int(row['rank']),5)]}")
        await m.answer("<b>🛡️ Модераторы по внутренним рангам</b>\n" + ("\n".join(rows) if rows else "Ранги модераторов пока не назначены."))
    except Exception:
        await m.answer("Не удалось получить список.")

async def moderation_target(m, command, minimum=3):
    if m.chat.type == ChatType.PRIVATE:
        await m.answer("Команда модерации работает в группе.")
        return None, ""
    if not await allowed(m, minimum):
        await m.reply("❌ Недостаточно прав.")
        return None, ""
    args = (m.text or "")[len(command):].strip()
    target, rest = await get_target(m, args)
    if not target and m.reply_to_message and m.reply_to_message.from_user:
        target = m.reply_to_message.from_user
    if not target:
        await m.reply("Укажи @username/ID или ответь на сообщение пользователя.")
        return None, ""
    if target.id == OWNER_ID and m.from_user.id != OWNER_ID:
        await m.reply("❌ Нельзя применять действие к владельцу бота.")
        return None, ""
    if target.id == m.from_user.id:
        await m.reply("❌ Нельзя применять это действие к себе.")
        return None, ""
    return target, rest

@dp.message(F.text.regexp(r"(?i)^(мут|mute)(\s|$)"))
async def mute(m: Message):
    target, rest = await moderation_target(m, m.text.split()[0], 3)
    if not target: return
    parts = rest.split(maxsplit=1)
    duration = parse_duration(parts[0]) if parts else None
    if not duration:
        await m.reply("Формат: <code>мут @user 10м причина</code> (срок: 1с–366д).")
        return
    reason = parts[1] if len(parts) > 1 else "не указана"
    try:
        await bot.restrict_chat_member(m.chat.id, target.id,
            permissions=ChatPermissions(can_send_messages=False),
            until_date=datetime.now(timezone.utc) + duration)
        audit(m.chat.id, m.from_user.id, "mute", target.id, reason)
        await m.answer(f"🔇 {mention(target.id, getattr(target, 'first_name', 'Пользователь'))} замучен на {duration}. Причина: {escape(reason)}")
    except Exception as e:
        await m.reply("Не удалось выдать мут. Проверь права бота и статус цели.")

@dp.message(F.text.regexp(r"(?i)^(размут|анмут|unmute)(\s|$)"))
async def unmute(m: Message):
    target, _ = await moderation_target(m, m.text.split()[0], 3)
    if not target: return
    try:
        await bot.restrict_chat_member(m.chat.id, target.id, permissions=ChatPermissions(
            can_send_messages=True, can_send_audios=True, can_send_documents=True,
            can_send_photos=True, can_send_videos=True, can_send_video_notes=True,
            can_send_voice_notes=True, can_send_polls=True, can_send_other_messages=True,
            can_add_web_page_previews=True))
        audit(m.chat.id, m.from_user.id, "unmute", target.id)
        await m.answer(f"🔊 Ограничения сняты с {mention(target.id)}.")
    except Exception:
        await m.reply("Не удалось снять мут. Проверь права бота.")

@dp.message(F.text.regexp(r"(?i)^(бан|ban)(\s|$)"))
async def ban(m: Message):
    target, reason = await moderation_target(m, m.text.split()[0], 3)
    if not target: return
    try:
        await bot.ban_chat_member(m.chat.id, target.id)
        audit(m.chat.id, m.from_user.id, "ban", target.id, reason)
        await m.answer(f"🔨 {mention(target.id)} забанен. Причина: {escape(reason or 'не указана')}")
    except Exception:
        await m.reply("Не удалось забанить. Проверь права бота и статус цели.")

@dp.message(F.text.regexp(r"(?i)^(разбан|unban)(\s|$)"))
async def unban(m: Message):
    if not await allowed(m, 3):
        await m.reply("❌ Недостаточно прав."); return
    args = m.text.split(maxsplit=1)
    if len(args) < 2:
        await m.reply("Формат: <code>разбан ID_пользователя</code>"); return
    try:
        uid = int(args[1].strip().lstrip("@"))
        await bot.unban_chat_member(m.chat.id, uid, only_if_banned=True)
        audit(m.chat.id, m.from_user.id, "unban", uid)
        await m.answer("✅ Пользователь разбанен.")
    except Exception:
        await m.reply("Не удалось разбанить. Используй числовой ID и проверь права.")

@dp.message(F.text.regexp(r"(?i)^(кик|kick)(\s|$)"))
async def kick(m: Message):
    target, reason = await moderation_target(m, m.text.split()[0], 3)
    if not target: return
    try:
        await bot.ban_chat_member(m.chat.id, target.id)
        await bot.unban_chat_member(m.chat.id, target.id)
        audit(m.chat.id, m.from_user.id, "kick", target.id, reason)
        await m.answer(f"👢 {mention(target.id)} удалён из чата. Причина: {escape(reason or 'не указана')}")
    except Exception:
        await m.reply("Не удалось удалить пользователя. Проверь права бота.")

@dp.message(F.text.regexp(r"(?i)^(варн|warn)(\s|$)"))
async def warn(m: Message):
    target, reason = await moderation_target(m, m.text.split()[0], 3)
    if not target: return
    row = user_record(target.id, m.chat.id, getattr(target, "username", None), getattr(target, "first_name", "Пользователь"))
    row["warns"] = int(row.get("warns", 0)) + 1
    save_user(row)
    audit(m.chat.id, m.from_user.id, "warn", target.id, reason)
    await m.answer(f"⚠️ {mention(target.id)} получил предупреждение. Всего: <b>{row['warns']}</b>. Причина: {escape(reason or 'не указана')}")

@dp.message(F.text.regexp(r"(?i)^(снять варн|унварн|анварн)(\s|$)"))
async def unwarn(m: Message):
    target, _ = await moderation_target(m, m.text.split()[0], 3)
    if not target: return
    row = user_record(target.id, m.chat.id, getattr(target, "username", None), getattr(target, "first_name", "Пользователь"))
    row["warns"] = max(0, int(row.get("warns", 0)) - 1)
    save_user(row)
    audit(m.chat.id, m.from_user.id, "unwarn", target.id)
    await m.answer(f"✅ Варны сняты. Осталось: <b>{row['warns']}</b>.")

@dp.message(F.text.startswith("-смс"))
async def delete_message(m: Message):
    if not await allowed(m, 3):
        await m.reply("❌ Недостаточно прав."); return
    if not m.reply_to_message:
        await m.reply("Ответь командой <code>-смс</code> на сообщение, которое нужно удалить."); return
    try:
        await m.reply_to_message.delete()
        await m.delete()
        audit(m.chat.id, m.from_user.id, "delete", m.reply_to_message.from_user.id if m.reply_to_message.from_user else None)
    except Exception:
        await m.reply("Не удалось удалить сообщение. Проверь право «Удаление сообщений».")

@dp.message(F.text.regexp(r"(?i)^(\.пин|pin)(\s|$)"))
async def pin(m: Message):
    if not await allowed(m, 3):
        await m.reply("❌ Недостаточно прав."); return
    if not m.reply_to_message:
        await m.reply("Ответь на сообщение командой <code>.пин</code>."); return
    try:
        await bot.pin_chat_message(m.chat.id, m.reply_to_message.message_id)
        await m.reply("📌 Сообщение закреплено.")
    except Exception:
        await m.reply("Не удалось закрепить. Проверь право закрепления сообщений.")

@dp.message(F.text.regexp(r"(?i)^(\.анпин|unpin)(\s|$)"))
async def unpin(m: Message):
    if not await allowed(m, 3):
        await m.reply("❌ Недостаточно прав."); return
    try:
        await bot.unpin_chat_message(m.chat.id)
        await m.reply("📌 Закрепление снято.")
    except Exception:
        await m.reply("Не удалось открепить сообщение.")

@dp.message(F.text.regexp(r"(?i)^(очистить|clear)\s+\d+"))
async def clear(m: Message):
    if not await allowed(m, 3):
        await m.reply("❌ Недостаточно прав."); return
    n = int(m.text.split()[1])
    if not 1 <= n <= 100:
        await m.reply("Можно удалить от 1 до 100 последних сообщений."); return
    deleted = 0
    for mid in range(m.message_id, max(0, m.message_id - n), -1):
        try:
            await bot.delete_message(m.chat.id, mid); deleted += 1
        except Exception:
            pass
    await m.answer(f"🧹 Удалено сообщений: {deleted}.")

@dp.message(F.text.regexp(r"(?i)^(повысить|понизить)(\s|$)"))
async def rank_change(m: Message):
    command = m.text.split()[0].lower()
    if not await allowed(m, 4):
        await m.reply("❌ Нужен старший модератор/администратор."); return
    target, _ = await get_target(m, m.text[len(command):].strip())
    if not target:
        await m.reply("Укажи @username или ответь на сообщение."); return
    row = user_record(target.id, m.chat.id, getattr(target, "username", None), getattr(target, "first_name", "Пользователь"))
    rank = max(0, min(int(row.get("rank", 0)) + (1 if command == "повысить" else -1), len(RANKS)-1))
    if target.id == OWNER_ID and m.from_user.id != OWNER_ID:
        await m.reply("Нельзя менять ранг владельца."); return
    row["rank"] = rank; save_user(row)
    audit(m.chat.id, m.from_user.id, command, target.id, RANKS[rank])
    await m.answer(f"🏅 Ранг {mention(target.id)}: <b>{RANKS[rank]}</b>.")

@dp.message(F.new_chat_members)
async def welcome(m: Message):
    for u in m.new_chat_members:
        if u.is_bot: continue
        user_record(u.id, m.chat.id, u.username, u.first_name)
        await m.answer(f"👋 Добро пожаловать, {u.mention_html()}!")

@dp.message()
async def count_messages(m: Message):
    if not m.from_user or m.from_user.is_bot or m.chat.type == ChatType.PRIVATE:
        return
    row = user_record(m.from_user.id, m.chat.id, m.from_user.username, m.from_user.first_name)
    row["messages"] = int(row.get("messages", 0)) + 1
    save_user(row)

async def main():
    log.info("Bot starting")
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
