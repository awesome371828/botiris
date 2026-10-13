"""
Модуль модерации. Этап 1: базовые команды + АНМУТ + АНВАРН.
"""

import re
from datetime import datetime, timedelta
from aiogram import Router, types
from aiogram.types import ChatPermissions
from database.crud import (
    get_user, create_user, add_warn, reset_warns, get_chat, set_rank,
)
from middlewares.check_rank import has_rank, get_user_rank, OWNER_ID

router = Router()

RANK_NAMES = {
    0: "Участник",
    1: "Младший модератор",
    2: "Старший модератор",
    3: "Младший администратор",
    4: "Старший администратор",
    5: "Создатель",
}

FULL_PERMISSIONS = ChatPermissions(
    can_send_messages=True,
    can_send_media_messages=True,
    can_send_other_messages=True,
    can_add_web_page_previews=True,
    can_send_polls=True,
    can_change_info=False,
    can_invite_users=True,
    can_pin_messages=False,
)

# ==================== ПАРСЕР ВРЕМЕНИ ====================
def parse_time(text: str):
    if not text:
        return None
    text = text.lower().strip()

    time_units = {
        "сек": 1, "секунда": 1, "секунды": 1, "секунд": 1, "с": 1,
        "мин": 60, "минута": 60, "минуты": 60, "минут": 60, "м": 60,
        "час": 3600, "часа": 3600, "часов": 3600, "ч": 3600,
        "день": 86400, "дня": 86400, "дней": 86400, "д": 86400, "сутки": 86400,
        "нед": 604800, "неделя": 604800, "недели": 604800, "недель": 604800, "н": 604800,
        "мес": 2592000, "месяц": 2592000, "месяца": 2592000, "месяцев": 2592000,
        "год": 31536000, "года": 31536000, "лет": 31536000, "г": 31536000,
    }

    match = re.match(r"^(\d+)\s*([а-яa-z]+)$", text)
    if match:
        num = int(match.group(1))
        unit = match.group(2)
        for key, multiplier in time_units.items():
            if unit.startswith(key) or key.startswith(unit):
                return timedelta(seconds=num * multiplier)
        return None

    match = re.match(r"^(\d+)", text)
    if not match:
        return None

    num = int(match.group(1))
    rest = text[match.end():].strip()
    for unit, multiplier in time_units.items():
        if unit in rest:
            return timedelta(seconds=num * multiplier)

    return None


# ==================== ИСПРАВЛЕННЫЙ ПАРСЕР СРОКА И ПРИЧИНЫ ====================
# Поддерживает: «мут 1 минута спам», «мут 1 час спам»,
# «мут 1 день 2 часа флуд», «мут 1г спам».
_DURATION_UNIT_SECONDS = {
    "с": 1, "сек": 1, "секунда": 1, "секунды": 1, "секунд": 1,
    "м": 60, "мин": 60, "минута": 60, "минуты": 60, "минут": 60,
    "ч": 3600, "час": 3600, "часа": 3600, "часов": 3600,
    "д": 86400, "день": 86400, "дня": 86400, "дней": 86400, "сутки": 86400, "суток": 86400,
    "н": 604800, "нед": 604800, "неделя": 604800, "недели": 604800, "недель": 604800,
    "мес": 2592000, "месяц": 2592000, "месяца": 2592000, "месяцев": 2592000,
    "г": 31536000, "год": 31536000, "года": 31536000, "лет": 31536000,
}

def _unit_seconds(token: str):
    token = token.lower().strip().rstrip(".,")
    if token in _DURATION_UNIT_SECONDS:
        return _DURATION_UNIT_SECONDS[token]
    # Support common abbreviated variants without matching a single letter inside a word.
    aliases = {"секунд": 1, "секун": 1, "минут": 60, "мину": 60, "час": 3600, "дне": 86400, "недел": 604800, "месяц": 2592000, "год": 31536000}
    for prefix, seconds in aliases.items():
        if len(token) >= 3 and prefix.startswith(token):
            return seconds
    return None

def _is_duration_unit(token: str) -> bool:
    return _unit_seconds(token) is not None

def parse_duration_reason(message: types.Message):
    """Возвращает (timedelta|None, текст_срока, причина).

    Если срока нет, мут бессрочный, а весь хвост считается причиной.
    При ответе на сообщение аргументы начинаются сразу после команды.
    При формате «мут @user 1 час спам» пропускается @user.
    """
    raw = (message.text or "").strip()
    parts = raw.split()
    if len(parts) <= 1:
        return None, "бессрочно", "не указана"
    i = 1
    if not message.reply_to_message and len(parts) > 1 and (parts[1].startswith("@") or parts[1].isdigit()):
        i = 2
    tail = parts[i:]
    duration_parts = []
    total_seconds = 0
    j = 0
    while j < len(tail):
        token = tail[j].strip(",")
        # Компактная форма: 10с, 5м, 1ч, 2д, 1г.
        compact = re.fullmatch(r"(\d+)\s*([а-яa-z]+)", token, re.IGNORECASE)
        if compact and _unit_seconds(compact.group(2)) is not None:
            duration_parts.append(token)
            total_seconds += int(compact.group(1)) * _unit_seconds(compact.group(2))
            j += 1
            continue
        # Раздельная форма: 1 час, 30 минут.
        if token.isdigit() and j + 1 < len(tail) and _is_duration_unit(tail[j + 1]):
            unit = tail[j + 1].strip(",")
            duration_parts.extend((token, unit))
            total_seconds += int(token) * _unit_seconds(unit)
            j += 2
            continue
        break
    duration_text = " ".join(duration_parts).strip()
    duration = timedelta(seconds=total_seconds) if total_seconds > 0 else None
    reason = " ".join(tail[j:]).strip(" ,.-") or "не указана"
    # Не превращаем непонятный срок в бессрочный с потерей введённого текста:
    # неизвестный формат считается причиной, а срок остаётся бессрочным.
    if duration is None:
        reason = " ".join(tail).strip(" ,.-") or "не указана"
        return None, "бессрочно", reason
    return duration, duration_text, reason


async def is_moderator(message: types.Message, required: int = 1) -> bool:
    if message.from_user.id == OWNER_ID:
        return True
    return await has_rank(message, required)


async def get_target(message: types.Message):
    if message.reply_to_message:
        return message.reply_to_message.from_user

    args = message.text.split()
    if len(args) < 2:
        return None

    target_username = args[1].lstrip("@")
    try:
        member = await message.chat.get_member(target_username)
        return member.user
    except Exception:
        return None


def get_duration_arg(message: types.Message):
    if not message.text:
        return None
    args = message.text.split()
    start_index = 1
    if len(args) > 1 and args[1].startswith("@"):
        start_index = 2
    time_parts = args[start_index:]
    if not time_parts:
        return None
    time_str = " ".join(time_parts)
    if not re.search(r"\d", time_str):
        return None
    return time_str


# ==================== МУТ ====================
@router.message(lambda m: m.text and m.text.lower().startswith((
    "мут", ".мут", "!мут", "/мут", "mute", ".mute", "!mute", "/mute"
)))
async def cmd_mute(message: types.Message):
    if not await is_moderator(message, 1):
        await message.reply("❌ Недостаточно прав.")
        return

    target = await get_target(message)
    if not target:
        await message.reply("❌ Ответьте на сообщение или укажите @username.")
        return
    if target.id == OWNER_ID:
        await message.reply("❌ Нельзя замутить владельца.")
        return
    if target.id == message.from_user.id:
        await message.reply("❌ Нельзя замутить самого себя.")
        return

    duration, time_text, reason = parse_duration_reason(message)
    until = datetime.now() + duration if duration else datetime.now() + timedelta(days=3650)

    try:
        await message.bot.restrict_chat_member(
            chat_id=message.chat.id,
            user_id=target.id,
            permissions=ChatPermissions(can_send_messages=False),
            until_date=until,
        )
        try:
            log_action(message.chat.id, "mutes", message.from_user.id, target.id, time=time_text, reason=reason)
        except Exception:
            pass
        await message.reply(f"🔇 {target.mention_html()} замучен на {time_text}. Причина: {reason}.")
    except Exception as e:
        await message.reply(f"❌ Ошибка: {e}")


# ==================== РАЗМУТ + АНМУТ ====================
@router.message(lambda m: m.text and m.text.lower().startswith((
    "размут", ".размут", "!размут", "/размут",
    "унмут", ".унмут", "!унмут", "/унмут",
    "анмут", ".анмут", "!анмут", "/анмут",
    "unmute", ".unmute", "!unmute", "/unmute"
)))
async def cmd_unmute(message: types.Message):
    if not await is_moderator(message, 1):
        await message.reply("❌ Недостаточно прав.")
        return

    target = await get_target(message)
    if not target:
        await message.reply("❌ Ответьте на сообщение или укажите @username.")
        return

    try:
        await message.bot.restrict_chat_member(
            chat_id=message.chat.id,
            user_id=target.id,
            permissions=FULL_PERMISSIONS,
        )
        await message.reply(f"🔊 {target.mention_html()} размучен (анмут).")
    except Exception as e:
        await message.reply(f"❌ Ошибка: {e}")


# ==================== БАН ====================
@router.message(lambda m: m.text and m.text.lower().startswith((
    "бан", ".бан", "!бан", "/бан", "ban", ".ban", "!ban", "/ban"
)))
async def cmd_ban(message: types.Message):
    if not await is_moderator(message, 2):
        await message.reply("❌ Недостаточно прав.")
        return

    target = await get_target(message)
    if not target:
        await message.reply("❌ Ответьте на сообщение или укажите @username.")
        return
    if target.id == OWNER_ID:
        await message.reply("❌ Нельзя забанить владельца.")
        return

    time_arg = get_duration_arg(message)
    duration = parse_time(time_arg) if time_arg else None

    try:
        await message.bot.ban_chat_member(message.chat.id, target.id)
        if duration:
            await message.reply(f"🔨 {target.mention_html()} забанен на {time_arg}.")
        else:
            await message.reply(f"🔨 {target.mention_html()} забанен навсегда.")
    except Exception as e:
        await message.reply(f"❌ Ошибка: {e}")


# ==================== РАЗБАН ====================
@router.message(lambda m: m.text and m.text.lower().startswith((
    "разбан", ".разбан", "!разбан", "/разбан",
    "унбан", ".унбан", "!унбан", "/унбан",
    "unban", ".unban", "!unban", "/unban"
)))
async def cmd_unban(message: types.Message):
    if not await is_moderator(message, 2):
        await message.reply("❌ Недостаточно прав.")
        return

    target = await get_target(message)
    if not target:
        await message.reply("❌ Ответьте на сообщение или укажите @username.")
        return

    try:
        await message.bot.unban_chat_member(message.chat.id, target.id)
        await message.reply(f"✅ {target.mention_html()} разбанен.")
    except Exception as e:
        await message.reply(f"❌ Ошибка: {e}")


# ==================== КИК ====================
@router.message(lambda m: m.text and m.text.lower().startswith((
    "кик", ".кик", "!кик", "/кик", "kick", ".kick", "!kick", "/kick"
)))
async def cmd_kick(message: types.Message):
    if not await is_moderator(message, 1):
        await message.reply("❌ Недостаточно прав.")
        return

    target = await get_target(message)
    if not target:
        await message.reply("❌ Ответьте на сообщение или укажите @username.")
        return
    if target.id == OWNER_ID:
        await message.reply("❌ Нельзя кикнуть владельца.")
        return

    try:
        await message.bot.ban_chat_member(message.chat.id, target.id)
        await message.bot.unban_chat_member(message.chat.id, target.id)
        await message.reply(f"👢 {target.mention_html()} кикнут.")
    except Exception as e:
        await message.reply(f"❌ Ошибка: {e}")


# ==================== ВАРН ====================
@router.message(lambda m: m.text and m.text.lower().startswith((
    "варн", ".варн", "!варн", "/варн",
    "пред", ".пред", "!пред", "/пред",
    "warn", ".warn", "!warn", "/warn"
)))
async def cmd_warn(message: types.Message):
    if not await is_moderator(message, 1):
        await message.reply("❌ Недостаточно прав.")
        return

    target = await get_target(message)
    if not target:
        await message.reply("❌ Ответьте на сообщение или укажите @username.")
        return
    if target.id == OWNER_ID:
        await message.reply("❌ Нельзя выдать варн владельцу.")
        return

    warns = add_warn(target.id, message.chat.id)
    if warns is None:
        create_user(target.id, message.chat.id, target.username, target.first_name)
        warns = add_warn(target.id, message.chat.id)

    await message.reply(f"⚠️ {target.mention_html()} получил варн. Всего: {warns}")

    chat = get_chat(message.chat.id)
    limit = chat["warn_limit"] if chat else 3

    if warns >= limit:
        try:
            await message.bot.restrict_chat_member(
                chat_id=message.chat.id,
                user_id=target.id,
                permissions=ChatPermissions(can_send_messages=False),
                until_date=datetime.now() + timedelta(hours=1),
            )
            await message.answer(f"🔨 {target.mention_html()} замучен на 1 час (лимит варнов).")
            reset_warns(target.id, message.chat.id)
        except Exception as e:
            await message.answer(f"❌ Ошибка: {e}")


# ==================== СНЯТЬ ВАРН + АНВАРН ====================
@router.message(lambda m: m.text and m.text.lower().startswith((
    "унварн", ".унварн", "!унварн", "/унварн",
    "анварн", ".анварн", "!анварн", "/анварн",
    "снятьварн", ".снятьварн", "!снятьварн", "/снятьварн",
    "unwarn", ".unwarn", "!unwarn", "/unwarn"
)))
async def cmd_unwarn(message: types.Message):
    if not await is_moderator(message, 2):
        await message.reply("❌ Недостаточно прав.")
        return

    target = await get_target(message)
    if not target:
        await message.reply("❌ Ответьте на сообщение или укажите @username.")
        return

    reset_warns(target.id, message.chat.id)
    await message.reply(f"✅ Варны {target.mention_html()} сняты (анварн).")


# ==================== ПРОВЕРКА ВАРНОВ ====================
@router.message(lambda m: m.text and m.text.lower().startswith((
    "варны", ".варны", "!варны", "/варны",
    "преды", ".преды", "!преды", "/преды",
    "warns", ".warns", "!warns", "/warns"
)))
async def cmd_warns(message: types.Message):
    target = await get_target(message)
    if not target:
        target = message.from_user

    user = get_user(target.id, message.chat.id)
    if not user:
        await message.reply(f"У {target.mention_html()} нет предупреждений.")
        return

    await message.reply(f"⚠️ {target.mention_html()}: <b>{user['warns']}</b> предупреждений.")


# ==================== ПОВЫШЕНИЕ ====================
@router.message(lambda m: m.text and m.text.lower().startswith((
    "повысить", ".повысить", "!повысить", "/повысить",
    "promote", ".promote", "!promote", "/promote"
)))
async def cmd_promote(message: types.Message):
    if not await is_moderator(message, 3):
        await message.reply("❌ Недостаточно прав.")
        return

    target = await get_target(message)
    if not target:
        await message.reply("❌ Ответьте на сообщение или укажите @username.")
        return

    current = await get_user_rank(target.id, message.chat.id)
    if current >= 5:
        await message.reply("Максимальный ранг.")
        return

    set_rank(target.id, message.chat.id, current + 1)
    await message.reply(f"⬆️ {target.mention_html()} повышен до <b>{RANK_NAMES[current + 1]}</b>")


# ==================== ПОНИЖЕНИЕ ====================
@router.message(lambda m: m.text and m.text.lower().startswith((
    "понизить", ".понизить", "!понизить", "/понизить",
    "demote", ".demote", "!demote", "/demote"
)))
async def cmd_demote(message: types.Message):
    if not await is_moderator(message, 4):
        await message.reply("❌ Недостаточно прав.")
        return

    target = await get_target(message)
    if not target:
        await message.reply("❌ Ответьте на сообщение или укажите @username.")
        return

    current = await get_user_rank(target.id, message.chat.id)
    if current <= 0:
        return

    set_rank(target.id, message.chat.id, current - 1)
    await message.reply(f"⬇️ {target.mention_html()} понижен до <b>{RANK_NAMES[current - 1]}</b>")


# ==================== МОЙ РАНГ ====================
@router.message(lambda m: m.text and m.text.lower().startswith((
    "мойранг", ".мойранг", "!мойранг", "/мойранг",
    "rank", ".rank", "!rank", "/rank",
    "ктоя", ".ктоя", "!ктоя", "/ктоя"
)))
async def cmd_rank(message: types.Message):
    target = message.reply_to_message.from_user if message.reply_to_message else message.from_user
    rank = await get_user_rank(target.id, message.chat.id)
    await message.reply(f"Ранг {target.mention_html()}: <b>{RANK_NAMES.get(rank, 'Участник')}</b>")
    """
Модуль модерации. Этап 2: полный парсер сроков + автоматические фильтры.
"""

import re
from datetime import datetime, timedelta
from collections import defaultdict
from aiogram import Router, types
from aiogram.types import ChatPermissions
from database.crud import (
    get_user, create_user, add_warn, reset_warns, get_chat, set_rank,
)
from middlewares.check_rank import has_rank, get_user_rank, OWNER_ID

# PATCH: router reset removed; all handlers share the first router.

RANK_NAMES = {
    0: "Участник",
    1: "Младший модератор",
    2: "Старший модератор",
    3: "Младший администратор",
    4: "Старший администратор",
    5: "Создатель",
}

FULL_PERMISSIONS = ChatPermissions(
    can_send_messages=True,
    can_send_media_messages=True,
    can_send_other_messages=True,
    can_add_web_page_previews=True,
    can_send_polls=True,
    can_change_info=False,
    can_invite_users=True,
    can_pin_messages=False,
)

# ==================== ХРАНИЛИЩЕ ФИЛЬТРОВ (в памяти) ====================
# В продакшене лучше хранить в БД, но для старта хватит памяти
chat_filters = defaultdict(lambda: {
    "antilink": False,
    "antimat": False,
    "antiflood": False,
    "antiflood_limit": 5,
    "antiflood_window": 10,  # секунд
    "anticaps": False,
    "anticaps_percent": 70,
    "antibot": False,
})

# Хранилище последних сообщений для антифлуда
flood_tracker = defaultdict(lambda: defaultdict(list))

# Список матерных слов (можно расширять)
BAD_WORDS = [
    "хуй", "пизд", "бляд", "ебал", "ебат", "сука", "блядь", "мраз",
    "fuck", "shit", "bitch", "asshole", "dick", "pussy",
]

# Регулярка для ссылок
URL_PATTERN = re.compile(
    r"(https?://|www\.|t\.me/|telegram\.me/|@[a-zA-Z0-9_]{5,})",
    re.IGNORECASE,
)

# ==================== ПАРСЕР ВРЕМЕНИ ====================
def parse_time(text: str):
    """Полный парсер: 10с, 5м, 1 час, 2 недели, 1 месяц, 1 год."""
    if not text:
        return None
    text = text.lower().strip()

    time_units = {
        "сек": 1, "секунда": 1, "секунды": 1, "секунд": 1, "с": 1,
        "мин": 60, "минута": 60, "минуты": 60, "минут": 60, "м": 60,
        "час": 3600, "часа": 3600, "часов": 3600, "ч": 3600,
        "день": 86400, "дня": 86400, "дней": 86400, "д": 86400, "сутки": 86400,
        "нед": 604800, "неделя": 604800, "недели": 604800, "недель": 604800, "н": 604800,
        "мес": 2592000, "месяц": 2592000, "месяца": 2592000, "месяцев": 2592000,
        "год": 31536000, "года": 31536000, "лет": 31536000, "г": 31536000,
    }

    match = re.match(r"^(\d+)\s*([а-яa-z]+)$", text)
    if match:
        num = int(match.group(1))
        unit = match.group(2)
        for key, multiplier in time_units.items():
            if unit.startswith(key) or key.startswith(unit):
                return timedelta(seconds=num * multiplier)
        return None

    match = re.match(r"^(\d+)", text)
    if not match:
        return None

    num = int(match.group(1))
    rest = text[match.end():].strip()
    for unit, multiplier in time_units.items():
        if unit in rest:
            return timedelta(seconds=num * multiplier)

    return None


async def is_moderator(message: types.Message, required: int = 1) -> bool:
    if message.from_user.id == OWNER_ID:
        return True
    return await has_rank(message, required)


async def get_target(message: types.Message):
    if message.reply_to_message:
        return message.reply_to_message.from_user

    args = message.text.split()
    if len(args) < 2:
        return None

    target_username = args[1].lstrip("@")
    try:
        member = await message.chat.get_member(target_username)
        return member.user
    except Exception:
        return None


def get_duration_arg(message: types.Message):
    if not message.text:
        return None
    args = message.text.split()
    start_index = 1
    if len(args) > 1 and args[1].startswith("@"):
        start_index = 2
    time_parts = args[start_index:]
    if not time_parts:
        return None
    time_str = " ".join(time_parts)
    if not re.search(r"\d", time_str):
        return None
    return time_str


# ==================== МУТ ====================
@router.message(lambda m: m.text and m.text.lower().startswith((
    "мут", ".мут", "!мут", "/мут", "mute", ".mute", "!mute", "/mute"
)))
async def cmd_mute(message: types.Message):
    if not await is_moderator(message, 1):
        await message.reply("❌ Недостаточно прав.")
        return

    target = await get_target(message)
    if not target:
        await message.reply("❌ Ответьте на сообщение или укажите @username.")
        return
    if target.id == OWNER_ID:
        await message.reply("❌ Нельзя замутить владельца.")
        return
    if target.id == message.from_user.id:
        await message.reply("❌ Нельзя замутить самого себя.")
        return

    duration, time_text, reason = parse_duration_reason(message)
    until = datetime.now() + duration if duration else datetime.now() + timedelta(days=3650)

    try:
        await message.bot.restrict_chat_member(
            chat_id=message.chat.id,
            user_id=target.id,
            permissions=ChatPermissions(can_send_messages=False),
            until_date=until,
        )
        try:
            log_action(message.chat.id, "mutes", message.from_user.id, target.id, time=time_text, reason=reason)
        except Exception:
            pass
        await message.reply(f"🔇 {target.mention_html()} замучен на {time_text}. Причина: {reason}.")
    except Exception as e:
        await message.reply(f"❌ Ошибка: {e}")


# ==================== РАЗМУТ + АНМУТ ====================
@router.message(lambda m: m.text and m.text.lower().startswith((
    "размут", ".размут", "!размут", "/размут",
    "унмут", ".унмут", "!унмут", "/унмут",
    "анмут", ".анмут", "!анмут", "/анмут",
    "unmute", ".unmute", "!unmute", "/unmute"
)))
async def cmd_unmute(message: types.Message):
    if not await is_moderator(message, 1):
        await message.reply("❌ Недостаточно прав.")
        return

    target = await get_target(message)
    if not target:
        await message.reply("❌ Ответьте на сообщение или укажите @username.")
        return

    try:
        await message.bot.restrict_chat_member(
            chat_id=message.chat.id,
            user_id=target.id,
            permissions=FULL_PERMISSIONS,
        )
        await message.reply(f"🔊 {target.mention_html()} размучен.")
    except Exception as e:
        await message.reply(f"❌ Ошибка: {e}")


# ==================== БАН ====================
@router.message(lambda m: m.text and m.text.lower().startswith((
    "бан", ".бан", "!бан", "/бан", "ban", ".ban", "!ban", "/ban"
)))
async def cmd_ban(message: types.Message):
    if not await is_moderator(message, 2):
        await message.reply("❌ Недостаточно прав.")
        return

    target = await get_target(message)
    if not target:
        await message.reply("❌ Ответьте на сообщение или укажите @username.")
        return
    if target.id == OWNER_ID:
        await message.reply("❌ Нельзя забанить владельца.")
        return

    time_arg = get_duration_arg(message)
    duration = parse_time(time_arg) if time_arg else None

    try:
        await message.bot.ban_chat_member(message.chat.id, target.id)
        if duration:
            await message.reply(f"🔨 {target.mention_html()} забанен на {time_arg}.")
        else:
            await message.reply(f"🔨 {target.mention_html()} забанен навсегда.")
    except Exception as e:
        await message.reply(f"❌ Ошибка: {e}")


# ==================== РАЗБАН ====================
@router.message(lambda m: m.text and m.text.lower().startswith((
    "разбан", ".разбан", "!разбан", "/разбан",
    "унбан", ".унбан", "!унбан", "/унбан",
    "unban", ".unban", "!unban", "/unban"
)))
async def cmd_unban(message: types.Message):
    if not await is_moderator(message, 2):
        await message.reply("❌ Недостаточно прав.")
        return

    target = await get_target(message)
    if not target:
        await message.reply("❌ Ответьте на сообщение или укажите @username.")
        return

    try:
        await message.bot.unban_chat_member(message.chat.id, target.id)
        await message.reply(f"✅ {target.mention_html()} разбанен.")
    except Exception as e:
        await message.reply(f"❌ Ошибка: {e}")


# ==================== КИК ====================
@router.message(lambda m: m.text and m.text.lower().startswith((
    "кик", ".кик", "!кик", "/кик", "kick", ".kick", "!kick", "/kick"
)))
async def cmd_kick(message: types.Message):
    if not await is_moderator(message, 1):
        await message.reply("❌ Недостаточно прав.")
        return

    target = await get_target(message)
    if not target:
        await message.reply("❌ Ответьте на сообщение или укажите @username.")
        return
    if target.id == OWNER_ID:
        await message.reply("❌ Нельзя кикнуть владельца.")
        return

    try:
        await message.bot.ban_chat_member(message.chat.id, target.id)
        await message.bot.unban_chat_member(message.chat.id, target.id)
        await message.reply(f"👢 {target.mention_html()} кикнут.")
    except Exception as e:
        await message.reply(f"❌ Ошибка: {e}")


# ==================== ВАРН ====================
@router.message(lambda m: m.text and m.text.lower().startswith((
    "варн", ".варн", "!варн", "/варн",
    "пред", ".пред", "!пред", "/пред",
    "warn", ".warn", "!warn", "/warn"
)))
async def cmd_warn(message: types.Message):
    if not await is_moderator(message, 1):
        await message.reply("❌ Недостаточно прав.")
        return

    target = await get_target(message)
    if not target:
        await message.reply("❌ Ответьте на сообщение или укажите @username.")
        return
    if target.id == OWNER_ID:
        await message.reply("❌ Нельзя выдать варн владельцу.")
        return

    warns = add_warn(target.id, message.chat.id)
    if warns is None:
        create_user(target.id, message.chat.id, target.username, target.first_name)
        warns = add_warn(target.id, message.chat.id)

    await message.reply(f"⚠️ {target.mention_html()} получил варн. Всего: {warns}")

    chat = get_chat(message.chat.id)
    limit = chat["warn_limit"] if chat else 3

    if warns >= limit:
        try:
            await message.bot.restrict_chat_member(
                chat_id=message.chat.id,
                user_id=target.id,
                permissions=ChatPermissions(can_send_messages=False),
                until_date=datetime.now() + timedelta(hours=1),
            )
            await message.answer(f"🔨 {target.mention_html()} замучен на 1 час (лимит варнов).")
            reset_warns(target.id, message.chat.id)
        except Exception as e:
            await message.answer(f"❌ Ошибка: {e}")


# ==================== СНЯТЬ ВАРН + АНВАРН ====================
@router.message(lambda m: m.text and m.text.lower().startswith((
    "унварн", ".унварн", "!унварн", "/унварн",
    "анварн", ".анварн", "!анварн", "/анварн",
    "снятьварн", ".снятьварн", "!снятьварн", "/снятьварн",
    "unwarn", ".unwarn", "!unwarn", "/unwarn"
)))
async def cmd_unwarn(message: types.Message):
    if not await is_moderator(message, 2):
        await message.reply("❌ Недостаточно прав.")
        return

    target = await get_target(message)
    if not target:
        await message.reply("❌ Ответьте на сообщение или укажите @username.")
        return

    reset_warns(target.id, message.chat.id)
    await message.reply(f"✅ Варны {target.mention_html()} сняты.")


# ==================== ПРОВЕРКА ВАРНОВ ====================
@router.message(lambda m: m.text and m.text.lower().startswith((
    "варны", ".варны", "!варны", "/варны",
    "преды", ".преды", "!преды", "/преды",
    "warns", ".warns", "!warns", "/warns"
)))
async def cmd_warns(message: types.Message):
    target = await get_target(message)
    if not target:
        target = message.from_user

    user = get_user(target.id, message.chat.id)
    if not user:
        await message.reply(f"У {target.mention_html()} нет предупреждений.")
        return

    await message.reply(f"⚠️ {target.mention_html()}: <b>{user['warns']}</b> предупреждений.")


# ==================== ПОВЫШЕНИЕ ====================
@router.message(lambda m: m.text and m.text.lower().startswith((
    "повысить", ".повысить", "!повысить", "/повысить",
    "promote", ".promote", "!promote", "/promote"
)))
async def cmd_promote(message: types.Message):
    if not await is_moderator(message, 3):
        await message.reply("❌ Недостаточно прав.")
        return

    target = await get_target(message)
    if not target:
        await message.reply("❌ Ответьте на сообщение или укажите @username.")
        return

    current = await get_user_rank(target.id, message.chat.id)
    if current >= 5:
        await message.reply("Максимальный ранг.")
        return

    set_rank(target.id, message.chat.id, current + 1)
    await message.reply(f"⬆️ {target.mention_html()} повышен до <b>{RANK_NAMES[current + 1]}</b>")


# ==================== ПОНИЖЕНИЕ ====================
@router.message(lambda m: m.text and m.text.lower().startswith((
    "понизить", ".понизить", "!понизить", "/понизить",
    "demote", ".demote", "!demote", "/demote"
)))
async def cmd_demote(message: types.Message):
    if not await is_moderator(message, 4):
        await message.reply("❌ Недостаточно прав.")
        return

    target = await get_target(message)
    if not target:
        await message.reply("❌ Ответьте на сообщение или укажите @username.")
        return

    current = await get_user_rank(target.id, message.chat.id)
    if current <= 0:
        return

    set_rank(target.id, message.chat.id, current - 1)
    await message.reply(f"⬇️ {target.mention_html()} понижен до <b>{RANK_NAMES[current - 1]}</b>")


# ==================== МОЙ РАНГ ====================
@router.message(lambda m: m.text and m.text.lower().startswith((
    "мойранг", ".мойранг", "!мойранг", "/мойранг",
    "rank", ".rank", "!rank", "/rank",
    "ктоя", ".ктоя", "!ктоя", "/ктоя"
)))
async def cmd_rank(message: types.Message):
    target = message.reply_to_message.from_user if message.reply_to_message else message.from_user
    rank = await get_user_rank(target.id, message.chat.id)
    await message.reply(f"Ранг {target.mention_html()}: <b>{RANK_NAMES.get(rank, 'Участник')}</b>")


# ==================== УПРАВЛЕНИЕ ФИЛЬТРАМИ ====================
@router.message(lambda m: m.text and m.text.lower().startswith((
    "+антиссылка", ".антиссылка", "!антиссылка",
    "+antilink", ".antilink", "!antilink"
)))
async def cmd_antilink_on(message: types.Message):
    if not await is_moderator(message, 2):
        await message.reply("❌ Недостаточно прав.")
        return
    chat_filters[message.chat.id]["antilink"] = True
    await message.reply("✅ Антиссылки включены.")


@router.message(lambda m: m.text and m.text.lower().startswith((
    "-антиссылка", ".антиссылка выкл", "!антиссылка выкл",
    "-antilink", ".antilink off", "!antilink off"
)))
async def cmd_antilink_off(message: types.Message):
    if not await is_moderator(message, 2):
        await message.reply("❌ Недостаточно прав.")
        return
    chat_filters[message.chat.id]["antilink"] = False
    await message.reply("❌ Антиссылки выключены.")


@router.message(lambda m: m.text and m.text.lower().startswith((
    "+антимат", ".антимат", "!антимат",
    "+antimat", ".antimat", "!antimat"
)))
async def cmd_antimat_on(message: types.Message):
    if not await is_moderator(message, 2):
        await message.reply("❌ Недостаточно прав.")
        return
    chat_filters[message.chat.id]["antimat"] = True
    await message.reply("✅ Антимат включён.")


@router.message(lambda m: m.text and m.text.lower().startswith((
    "-антимат", ".антимат выкл", "!антимат выкл",
    "-antimat", ".antimat off", "!antimat off"
)))
async def cmd_antimat_off(message: types.Message):
    if not await is_moderator(message, 2):
        await message.reply("❌ Недостаточно прав.")
        return
    chat_filters[message.chat.id]["antimat"] = False
    await message.reply("❌ Антимат выключен.")


@router.message(lambda m: m.text and m.text.lower().startswith((
    "+антифлуд", ".антифлуд", "!антифлуд",
    "+antiflood", ".antiflood", "!antiflood"
)))
async def cmd_antiflood_on(message: types.Message):
    if not await is_moderator(message, 2):
        await message.reply("❌ Недостаточно прав.")
        return
    chat_filters[message.chat.id]["antiflood"] = True
    await message.reply("✅ Антифлуд включён (5 сообщений / 10 секунд).")


@router.message(lambda m: m.text and m.text.lower().startswith((
    "-антифлуд", ".антифлуд выкл", "!антифлуд выкл",
    "-antiflood", ".antiflood off", "!antiflood off"
)))
async def cmd_antiflood_off(message: types.Message):
    if not await is_moderator(message, 2):
        await message.reply("❌ Недостаточно прав.")
        return
    chat_filters[message.chat.id]["antiflood"] = False
    await message.reply("❌ Антифлуд выключен.")


@router.message(lambda m: m.text and m.text.lower().startswith((
    "+антикапс", ".антикапс", "!антикапс",
    "+anticaps", ".anticaps", "!anticaps"
)))
async def cmd_anticaps_on(message: types.Message):
    if not await is_moderator(message, 2):
        await message.reply("❌ Недостаточно прав.")
        return
    chat_filters[message.chat.id]["anticaps"] = True
    await message.reply("✅ Антикапс включён (70% капса).")


@router.message(lambda m: m.text and m.text.lower().startswith((
    "-антикапс", ".антикапс выкл", "!антикапс выкл",
    "-anticaps", ".anticaps off", "!anticaps off"
)))
async def cmd_anticaps_off(message: types.Message):
    if not await is_moderator(message, 2):
        await message.reply("❌ Недостаточно прав.")
        return
    chat_filters[message.chat.id]["anticaps"] = False
    await message.reply("❌ Антикапс выключен.")


# ==================== АВТОМАТИЧЕСКИЕ ФИЛЬТРЫ ====================
# PATCH: earlier catch-all decorator removed so commands and final filters can run.
async def auto_filters(message: types.Message):
    """
    Проверяет каждое сообщение на:
    - антиссылки
    - антимат
    - антифлуд
    - антикапс
    """
    if not message.text:
        return

    # Пропускаем модераторов и владельца
    if message.from_user.id == OWNER_ID:
        return
    if await has_rank(message, 1):
        return

    chat_id = message.chat.id
    filters = chat_filters[chat_id]
    text = message.text

    # ===== АНТИССЫЛКИ =====
    if filters["antilink"]:
        if URL_PATTERN.search(text):
            try:
                await message.delete()
                await message.answer(
                    f"🔗 {message.from_user.mention_html()}, ссылки запрещены!"
                )
                return
            except Exception:
                pass

    # ===== АНТИМАТ =====
    if filters["antimat"]:
        lower_text = text.lower()
        for word in BAD_WORDS:
            if word in lower_text:
                try:
                    await message.delete()
                    await message.answer(
                        f"🤬 {message.from_user.mention_html()}, мат запрещён!"
                    )
                    return
                except Exception:
                    pass

    # ===== АНТИКАПС =====
    if filters["anticaps"] and len(text) > 5:
        letters = [c for c in text if c.isalpha()]
        if letters:
            caps = sum(1 for c in letters if c.isupper())
            percent = (caps / len(letters)) * 100
            if percent >= filters["anticaps_percent"]:
                try:
                    await message.delete()
                    await message.answer(
                        f"🔠 {message.from_user.mention_html()}, капс запрещён!"
                    )
                    return
                except Exception:
                    pass

    # ===== АНТИФЛУД =====
    if filters["antiflood"]:
        now = datetime.now()
        window = filters["antiflood_window"]
        limit = filters["antiflood_limit"]

        # Очищаем старые записи
        flood_tracker[chat_id][message.from_user.id] = [
            t for t in flood_tracker[chat_id][message.from_user.id]
            if (now - t).total_seconds() < window
        ]

        # Добавляем текущее
        flood_tracker[chat_id][message.from_user.id].append(now)

        # Проверяем лимит
        if len(flood_tracker[chat_id][message.from_user.id]) > limit:
            try:
                await message.bot.restrict_chat_member(
                    chat_id=chat_id,
                    user_id=message.from_user.id,
                    permissions=ChatPermissions(can_send_messages=False),
                    until_date=now + timedelta(minutes=5),
                )
                await message.answer(
                    f"🌊 {message.from_user.mention_html()} замучен на 5 минут за флуд."
                )
                flood_tracker[chat_id][message.from_user.id] = []
            except Exception:
                pass
                """
Модуль модерации. Этап 3: расширенные фильтры контента.
"""

import re
from datetime import datetime, timedelta
from collections import defaultdict
from aiogram import Router, types
from aiogram.types import ChatPermissions
from database.crud import (
    get_user, create_user, add_warn, reset_warns, get_chat, set_rank,
)
from middlewares.check_rank import has_rank, get_user_rank, OWNER_ID

# PATCH: router reset removed; all handlers share the first router.

RANK_NAMES = {
    0: "Участник",
    1: "Младший модератор",
    2: "Старший модератор",
    3: "Младший администратор",
    4: "Старший администратор",
    5: "Создатель",
}

FULL_PERMISSIONS = ChatPermissions(
    can_send_messages=True,
    can_send_media_messages=True,
    can_send_other_messages=True,
    can_add_web_page_previews=True,
    can_send_polls=True,
    can_change_info=False,
    can_invite_users=True,
    can_pin_messages=False,
)

# ==================== ХРАНИЛИЩЕ ФИЛЬТРОВ ====================
chat_filters = defaultdict(lambda: {
    # Текстовые фильтры
    "antilink": False,
    "antimat": False,
    "antiflood": False,
    "antiflood_limit": 5,
    "antiflood_window": 10,
    "anticaps": False,
    "anticaps_percent": 70,
    # Медиа-фильтры
    "antisticker": False,
    "antigif": False,
    "antiphoto": False,
    "antivideo": False,
    "antivoice": False,
    "antidocument": False,
    # Специальные фильтры
    "antibot": False,
    "antireklama": False,
    "antikripta": False,
    "antifishing": False,
    "antiinvite": False,
    "antiall": False,
    # Срок наказания при срабатывании фильтра (минуты)
    "filter_mute_time": 10,
})

# Хранилище последних сообщений для антифлуда
flood_tracker = defaultdict(lambda: defaultdict(list))

# ==================== СПИСКИ СЛОВ ====================
BAD_WORDS = [
    "хуй", "пизд", "бляд", "ебал", "ебат", "сука", "блядь", "мраз",
    "fuck", "shit", "bitch", "asshole", "dick", "pussy",
]

# Реклама: слова-маркеры
REKLAMA_WORDS = [
    "купить", "продам", "продаю", "скидка", "акция", "распродажа",
    "заработок", "заработать", "инвестиции", "вложить", "крипта",
    "btc", "bitcoin", "eth", "ethereum", "usdt", "toncoin", "ton",
    "казино", "ставки", "букмекер", "1xbet", "betting",
    "подпишись", "подписка", "канал", "чат", "переходи",
    "промокод", "реферал", "рефка", "приглашаю",
]

# Крипта: отдельный список
KRIPTA_WORDS = [
    "btc", "bitcoin", "eth", "ethereum", "usdt", "toncoin", "ton",
    "crypto", "крипта", "кошелёк", "wallet", "binance", "bybit",
    "p2p", "обменник", "майнинг", "mining", "нфт", "nft",
]

# Фишинг: подозрительные домены и паттерны
FISHING_PATTERNS = [
    r"t\.me/joinchat", r"telegram\.me/joinchat",
    r"bit\.ly", r"tinyurl", r"goo\.gl", r"shorturl",
    r"free.*ton", r"bonus.*ton", r"gift.*ton",
    r"airdrop", r"giveaway", r"claim.*reward",
    r"verify.*wallet", r"connect.*wallet", r"seed.*phrase",
]

# Регулярка для ссылок
URL_PATTERN = re.compile(
    r"(https?://|www\.|t\.me/|telegram\.me/)",
    re.IGNORECASE,
)

# Регулярка для @all / @everyone
ALL_PATTERN = re.compile(r"@(all|everyone|here)", re.IGNORECASE)


# ==================== ПАРСЕР ВРЕМЕНИ ====================
def parse_time(text: str):
    if not text:
        return None
    text = text.lower().strip()

    time_units = {
        "сек": 1, "секунда": 1, "секунды": 1, "секунд": 1, "с": 1,
        "мин": 60, "минута": 60, "минуты": 60, "минут": 60, "м": 60,
        "час": 3600, "часа": 3600, "часов": 3600, "ч": 3600,
        "день": 86400, "дня": 86400, "дней": 86400, "д": 86400, "сутки": 86400,
        "нед": 604800, "неделя": 604800, "недели": 604800, "недель": 604800, "н": 604800,
        "мес": 2592000, "месяц": 2592000, "месяца": 2592000, "месяцев": 2592000,
        "год": 31536000, "года": 31536000, "лет": 31536000, "г": 31536000,
    }

    match = re.match(r"^(\d+)\s*([а-яa-z]+)$", text)
    if match:
        num = int(match.group(1))
        unit = match.group(2)
        for key, multiplier in time_units.items():
            if unit.startswith(key) or key.startswith(unit):
                return timedelta(seconds=num * multiplier)
        return None

    match = re.match(r"^(\d+)", text)
    if not match:
        return None

    num = int(match.group(1))
    rest = text[match.end():].strip()
    for unit, multiplier in time_units.items():
        if unit in rest:
            return timedelta(seconds=num * multiplier)

    return None


async def is_moderator(message: types.Message, required: int = 1) -> bool:
    if message.from_user.id == OWNER_ID:
        return True
    return await has_rank(message, required)


async def get_target(message: types.Message):
    if message.reply_to_message:
        return message.reply_to_message.from_user

    args = message.text.split()
    if len(args) < 2:
        return None

    target_username = args[1].lstrip("@")
    try:
        member = await message.chat.get_member(target_username)
        return member.user
    except Exception:
        return None


def get_duration_arg(message: types.Message):
    if not message.text:
        return None
    args = message.text.split()
    start_index = 1
    if len(args) > 1 and args[1].startswith("@"):
        start_index = 2
    time_parts = args[start_index:]
    if not time_parts:
        return None
    time_str = " ".join(time_parts)
    if not re.search(r"\d", time_str):
        return None
    return time_str


async def apply_filter_punishment(message: types.Message, reason: str):
    """Применить наказание за срабатывание фильтра."""
    chat_id = message.chat.id
    mute_time = chat_filters[chat_id]["filter_mute_time"]

    try:
        await message.delete()
    except Exception:
        pass

    try:
        await message.bot.restrict_chat_member(
            chat_id=chat_id,
            user_id=message.from_user.id,
            permissions=ChatPermissions(can_send_messages=False),
            until_date=datetime.now() + timedelta(minutes=mute_time),
        )
        await message.answer(
            f"🚫 {message.from_user.mention_html()} замучен на {mute_time} мин. "
            f"Причина: {reason}"
        )
    except Exception:
        pass


# ==================== БАЗОВЫЕ КОМАНДЫ ====================
@router.message(lambda m: m.text and m.text.lower().startswith((
    "мут", ".мут", "!мут", "/мут", "mute", ".mute", "!mute", "/mute"
)))
async def cmd_mute(message: types.Message):
    if not await is_moderator(message, 1):
        await message.reply("❌ Недостаточно прав.")
        return

    target = await get_target(message)
    if not target:
        await message.reply("❌ Ответьте на сообщение или укажите @username.")
        return
    if target.id == OWNER_ID:
        await message.reply("❌ Нельзя замутить владельца.")
        return
    if target.id == message.from_user.id:
        await message.reply("❌ Нельзя замутить самого себя.")
        return

    duration, time_text, reason = parse_duration_reason(message)
    until = datetime.now() + duration if duration else datetime.now() + timedelta(days=3650)

    try:
        await message.bot.restrict_chat_member(
            chat_id=message.chat.id,
            user_id=target.id,
            permissions=ChatPermissions(can_send_messages=False),
            until_date=until,
        )
        try:
            log_action(message.chat.id, "mutes", message.from_user.id, target.id, time=time_text, reason=reason)
        except Exception:
            pass
        await message.reply(f"🔇 {target.mention_html()} замучен на {time_text}. Причина: {reason}.")
    except Exception as e:
        await message.reply(f"❌ Ошибка: {e}")


@router.message(lambda m: m.text and m.text.lower().startswith((
    "размут", ".размут", "!размут", "/размут",
    "унмут", ".унмут", "!унмут", "/унмут",
    "анмут", ".анмут", "!анмут", "/анмут",
    "unmute", ".unmute", "!unmute", "/unmute"
)))
async def cmd_unmute(message: types.Message):
    if not await is_moderator(message, 1):
        await message.reply("❌ Недостаточно прав.")
        return

    target = await get_target(message)
    if not target:
        await message.reply("❌ Ответьте на сообщение или укажите @username.")
        return

    try:
        await message.bot.restrict_chat_member(
            chat_id=message.chat.id,
            user_id=target.id,
            permissions=FULL_PERMISSIONS,
        )
        await message.reply(f"🔊 {target.mention_html()} размучен.")
    except Exception as e:
        await message.reply(f"❌ Ошибка: {e}")


@router.message(lambda m: m.text and m.text.lower().startswith((
    "бан", ".бан", "!бан", "/бан", "ban", ".ban", "!ban", "/ban"
)))
async def cmd_ban(message: types.Message):
    if not await is_moderator(message, 2):
        await message.reply("❌ Недостаточно прав.")
        return

    target = await get_target(message)
    if not target:
        await message.reply("❌ Ответьте на сообщение или укажите @username.")
        return
    if target.id == OWNER_ID:
        await message.reply("❌ Нельзя забанить владельца.")
        return

    time_arg = get_duration_arg(message)
    duration = parse_time(time_arg) if time_arg else None

    try:
        await message.bot.ban_chat_member(message.chat.id, target.id)
        if duration:
            await message.reply(f"🔨 {target.mention_html()} забанен на {time_arg}.")
        else:
            await message.reply(f"🔨 {target.mention_html()} забанен навсегда.")
    except Exception as e:
        await message.reply(f"❌ Ошибка: {e}")


@router.message(lambda m: m.text and m.text.lower().startswith((
    "разбан", ".разбан", "!разбан", "/разбан",
    "унбан", ".унбан", "!унбан", "/унбан",
    "unban", ".unban", "!unban", "/unban"
)))
async def cmd_unban(message: types.Message):
    if not await is_moderator(message, 2):
        await message.reply("❌ Недостаточно прав.")
        return

    target = await get_target(message)
    if not target:
        await message.reply("❌ Ответьте на сообщение или укажите @username.")
        return

    try:
        await message.bot.unban_chat_member(message.chat.id, target.id)
        await message.reply(f"✅ {target.mention_html()} разбанен.")
    except Exception as e:
        await message.reply(f"❌ Ошибка: {e}")


@router.message(lambda m: m.text and m.text.lower().startswith((
    "кик", ".кик", "!кик", "/кик", "kick", ".kick", "!kick", "/kick"
)))
async def cmd_kick(message: types.Message):
    if not await is_moderator(message, 1):
        await message.reply("❌ Недостаточно прав.")
        return

    target = await get_target(message)
    if not target:
        await message.reply("❌ Ответьте на сообщение или укажите @username.")
        return
    if target.id == OWNER_ID:
        await message.reply("❌ Нельзя кикнуть владельца.")
        return

    try:
        await message.bot.ban_chat_member(message.chat.id, target.id)
        await message.bot.unban_chat_member(message.chat.id, target.id)
        await message.reply(f"👢 {target.mention_html()} кикнут.")
    except Exception as e:
        await message.reply(f"❌ Ошибка: {e}")


@router.message(lambda m: m.text and m.text.lower().startswith((
    "варн", ".варн", "!варн", "/варн",
    "пред", ".пред", "!пред", "/пред",
    "warn", ".warn", "!warn", "/warn"
)))
async def cmd_warn(message: types.Message):
    if not await is_moderator(message, 1):
        await message.reply("❌ Недостаточно прав.")
        return

    target = await get_target(message)
    if not target:
        await message.reply("❌ Ответьте на сообщение или укажите @username.")
        return
    if target.id == OWNER_ID:
        await message.reply("❌ Нельзя выдать варн владельцу.")
        return

    warns = add_warn(target.id, message.chat.id)
    if warns is None:
        create_user(target.id, message.chat.id, target.username, target.first_name)
        warns = add_warn(target.id, message.chat.id)

    await message.reply(f"⚠️ {target.mention_html()} получил варн. Всего: {warns}")

    chat = get_chat(message.chat.id)
    limit = chat["warn_limit"] if chat else 3

    if warns >= limit:
        try:
            await message.bot.restrict_chat_member(
                chat_id=message.chat.id,
                user_id=target.id,
                permissions=ChatPermissions(can_send_messages=False),
                until_date=datetime.now() + timedelta(hours=1),
            )
            await message.answer(f"🔨 {target.mention_html()} замучен на 1 час (лимит варнов).")
            reset_warns(target.id, message.chat.id)
        except Exception as e:
            await message.answer(f"❌ Ошибка: {e}")


@router.message(lambda m: m.text and m.text.lower().startswith((
    "унварн", ".унварн", "!унварн", "/унварн",
    "анварн", ".анварн", "!анварн", "/анварн",
    "снятьварн", ".снятьварн", "!снятьварн", "/снятьварн",
    "unwarn", ".unwarn", "!unwarn", "/unwarn"
)))
async def cmd_unwarn(message: types.Message):
    if not await is_moderator(message, 2):
        await message.reply("❌ Недостаточно прав.")
        return

    target = await get_target(message)
    if not target:
        await message.reply("❌ Ответьте на сообщение или укажите @username.")
        return

    reset_warns(target.id, message.chat.id)
    await message.reply(f"✅ Варны {target.mention_html()} сняты.")


@router.message(lambda m: m.text and m.text.lower().startswith((
    "варны", ".варны", "!варны", "/варны",
    "преды", ".преды", "!преды", "/преды",
    "warns", ".warns", "!warns", "/warns"
)))
async def cmd_warns(message: types.Message):
    target = await get_target(message)
    if not target:
        target = message.from_user

    user = get_user(target.id, message.chat.id)
    if not user:
        await message.reply(f"У {target.mention_html()} нет предупреждений.")
        return

    await message.reply(f"⚠️ {target.mention_html()}: <b>{user['warns']}</b> предупреждений.")


@router.message(lambda m: m.text and m.text.lower().startswith((
    "повысить", ".повысить", "!повысить", "/повысить",
    "promote", ".promote", "!promote", "/promote"
)))
async def cmd_promote(message: types.Message):
    if not await is_moderator(message, 3):
        await message.reply("❌ Недостаточно прав.")
        return

    target = await get_target(message)
    if not target:
        await message.reply("❌ Ответьте на сообщение или укажите @username.")
        return

    current = await get_user_rank(target.id, message.chat.id)
    if current >= 5:
        await message.reply("Максимальный ранг.")
        return

    set_rank(target.id, message.chat.id, current + 1)
    await message.reply(f"⬆️ {target.mention_html()} повышен до <b>{RANK_NAMES[current + 1]}</b>")


@router.message(lambda m: m.text and m.text.lower().startswith((
    "понизить", ".понизить", "!понизить", "/понизить",
    "demote", ".demote", "!demote", "/demote"
)))
async def cmd_demote(message: types.Message):
    if not await is_moderator(message, 4):
        await message.reply("❌ Недостаточно прав.")
        return

    target = await get_target(message)
    if not target:
        await message.reply("❌ Ответьте на сообщение или укажите @username.")
        return

    current = await get_user_rank(target.id, message.chat.id)
    if current <= 0:
        return

    set_rank(target.id, message.chat.id, current - 1)
    await message.reply(f"⬇️ {target.mention_html()} понижен до <b>{RANK_NAMES[current - 1]}</b>")


@router.message(lambda m: m.text and m.text.lower().startswith((
    "мойранг", ".мойранг", "!мойранг", "/мойранг",
    "rank", ".rank", "!rank", "/rank",
    "ктоя", ".ктоя", "!ктоя", "/ктоя"
)))
async def cmd_rank(message: types.Message):
    target = message.reply_to_message.from_user if message.reply_to_message else message.from_user
    rank = await get_user_rank(target.id, message.chat.id)
    await message.reply(f"Ранг {target.mention_html()}: <b>{RANK_NAMES.get(rank, 'Участник')}</b>")


# ==================== УПРАВЛЕНИЕ ФИЛЬТРАМИ (этап 2) ====================
@router.message(lambda m: m.text and m.text.lower().startswith(("+антиссылка", ".антиссылка", "!антиссылка")))
async def cmd_antilink_on(message: types.Message):
    if not await is_moderator(message, 2):
        return
    chat_filters[message.chat.id]["antilink"] = True
    await message.reply("✅ Антиссылки включены.")


@router.message(lambda m: m.text and m.text.lower().startswith(("-антиссылка", ".антиссылка выкл", "!антиссылка выкл")))
async def cmd_antilink_off(message: types.Message):
    if not await is_moderator(message, 2):
        return
    chat_filters[message.chat.id]["antilink"] = False
    await message.reply("❌ Антиссылки выключены.")


@router.message(lambda m: m.text and m.text.lower().startswith(("+антимат", ".антимат", "!антимат")))
async def cmd_antimat_on(message: types.Message):
    if not await is_moderator(message, 2):
        return
    chat_filters[message.chat.id]["antimat"] = True
    await message.reply("✅ Антимат включён.")


@router.message(lambda m: m.text and m.text.lower().startswith(("-антимат", ".антимат выкл", "!антимат выкл")))
async def cmd_antimat_off(message: types.Message):
    if not await is_moderator(message, 2):
        return
    chat_filters[message.chat.id]["antimat"] = False
    await message.reply("❌ Антимат выключен.")


@router.message(lambda m: m.text and m.text.lower().startswith(("+антифлуд", ".антифлуд", "!антифлуд")))
async def cmd_antiflood_on(message: types.Message):
    if not await is_moderator(message, 2):
        return
    chat_filters[message.chat.id]["antiflood"] = True
    await message.reply("✅ Антифлуд включён (5 сообщений / 10 секунд).")


@router.message(lambda m: m.text and m.text.lower().startswith(("-антифлуд", ".антифлуд выкл", "!антифлуд выкл")))
async def cmd_antiflood_off(message: types.Message):
    if not await is_moderator(message, 2):
        return
    chat_filters[message.chat.id]["antiflood"] = False
    await message.reply("❌ Антифлуд выключен.")


@router.message(lambda m: m.text and m.text.lower().startswith(("+антикапс", ".антикапс", "!антикапс")))
async def cmd_anticaps_on(message: types.Message):
    if not await is_moderator(message, 2):
        return
    chat_filters[message.chat.id]["anticaps"] = True
    await message.reply("✅ Антикапс включён.")


@router.message(lambda m: m.text and m.text.lower().startswith(("-антикапс", ".антикапс выкл", "!антикапс выкл")))
async def cmd_anticaps_off(message: types.Message):
    if not await is_moderator(message, 2):
        return
    chat_filters[message.chat.id]["anticaps"] = False
    await message.reply("❌ Антикапс выключен.")


# ==================== УПРАВЛЕНИЕ ФИЛЬТРАМИ (этап 3) ====================
@router.message(lambda m: m.text and m.text.lower().startswith(("+антибот", ".антибот", "!антибот")))
async def cmd_antibot_on(message: types.Message):
    if not await is_moderator(message, 2):
        return
    chat_filters[message.chat.id]["antibot"] = True
    await message.reply("✅ Антибот включён.")


@router.message(lambda m: m.text and m.text.lower().startswith(("-антибот", ".антибот выкл", "!антибот выкл")))
async def cmd_antibot_off(message: types.Message):
    if not await is_moderator(message, 2):
        return
    chat_filters[message.chat.id]["antibot"] = False
    await message.reply("❌ Антибот выключен.")


@router.message(lambda m: m.text and m.text.lower().startswith(("+антиреклама", ".антиреклама", "!антиреклама")))
async def cmd_antireklama_on(message: types.Message):
    if not await is_moderator(message, 2):
        return
    chat_filters[message.chat.id]["antireklama"] = True
    await message.reply("✅ Антиреклама включена.")


@router.message(lambda m: m.text and m.text.lower().startswith(("-антиреклама", ".антиреклама выкл", "!антиреклама выкл")))
async def cmd_antireklama_off(message: types.Message):
    if not await is_moderator(message, 2):
        return
    chat_filters[message.chat.id]["antireklama"] = False
    await message.reply("❌ Антиреклама выключена.")


@router.message(lambda m: m.text and m.text.lower().startswith(("+антикрипта", ".антикрипта", "!антикрипта")))
async def cmd_antikripta_on(message: types.Message):
    if not await is_moderator(message, 2):
        return
    chat_filters[message.chat.id]["antikripta"] = True
    await message.reply("✅ Антикрипта включена.")


@router.message(lambda m: m.text and m.text.lower().startswith(("-антикрипта", ".антикрипта выкл", "!антикрипта выкл")))
async def cmd_antikripta_off(message: types.Message):
    if not await is_moderator(message, 2):
        return
    chat_filters[message.chat.id]["antikripta"] = False
    await message.reply("❌ Антикрипта выключена.")


@router.message(lambda m: m.text and m.text.lower().startswith(("+антифишинг", ".антифишинг", "!антифишинг")))
async def cmd_antifishing_on(message: types.Message):
    if not await is_moderator(message, 2):
        return
    chat_filters[message.chat.id]["antifishing"] = True
    await message.reply("✅ Антифишинг включён.")


@router.message(lambda m: m.text and m.text.lower().startswith(("-антифишинг", ".антифишинг выкл", "!антифишинг выкл")))
async def cmd_antifishing_off(message: types.Message):
    if not await is_moderator(message, 2):
        return
    chat_filters[message.chat.id]["antifishing"] = False
    await message.reply("❌ Антифишинг выключен.")


@router.message(lambda m: m.text and m.text.lower().startswith(("+антивызов", ".антивызов", "!антивызов", "+антивызов", "+антиинвайт", ".антиинвайт", "!антиинвайт")))
async def cmd_antiinvite_on(message: types.Message):
    if not await is_moderator(message, 2):
        return
    chat_filters[message.chat.id]["antiinvite"] = True
    await message.reply("✅ Анти-инвайт включён.")


@router.message(lambda m: m.text and m.text.lower().startswith(("-антиинвайт", ".антиинвайт выкл", "!антиинвайт выкл")))
async def cmd_antiinvite_off(message: types.Message):
    if not await is_moderator(message, 2):
        return
    chat_filters[message.chat.id]["antiinvite"] = False
    await message.reply("❌ Анти-инвайт выключен.")


@router.message(lambda m: m.text and m.text.lower().startswith(("+антиолл", ".антиолл", "!антиолл", "+антиалл", ".антиалл", "!антиалл")))
async def cmd_antiall_on(message: types.Message):
    if not await is_moderator(message, 2):
        return
    chat_filters[message.chat.id]["antiall"] = True
    await message.reply("✅ Анти @all включён.")


@router.message(lambda m: m.text and m.text.lower().startswith(("-антиолл", ".антиолл выкл", "!антиолл выкл", "-антиалл", ".антиалл выкл", "!антиалл выкл")))
async def cmd_antiall_off(message: types.Message):
    if not await is_moderator(message, 2):
        return
    chat_filters[message.chat.id]["antiall"] = False
    await message.reply("❌ Анти @all выключен.")


# Медиа-фильтры
@router.message(lambda m: m.text and m.text.lower().startswith(("+антифото", ".антифото", "!антифото")))
async def cmd_antiphoto_on(message: types.Message):
    if not await is_moderator(message, 2):
        return
    chat_filters[message.chat.id]["antiphoto"] = True
    await message.reply("✅ Антифото включён.")


@router.message(lambda m: m.text and m.text.lower().startswith(("-антифото", ".антифото выкл", "!антифото выкл")))
async def cmd_antiphoto_off(message: types.Message):
    if not await is_moderator(message, 2):
        return
    chat_filters[message.chat.id]["antiphoto"] = False
    await message.reply("❌ Антифото выключен.")


@router.message(lambda m: m.text and m.text.lower().startswith(("+антивидео", ".антивидео", "!антивидео")))
async def cmd_antivideo_on(message: types.Message):
    if not await is_moderator(message, 2):
        return
    chat_filters[message.chat.id]["antivideo"] = True
    await message.reply("✅ Антивидео включён.")


@router.message(lambda m: m.text and m.text.lower().startswith(("-антивидео", ".антивидео выкл", "!антивидео выкл")))
async def cmd_antivideo_off(message: types.Message):
    if not await is_moderator(message, 2):
        return
    chat_filters[message.chat.id]["antivideo"] = False
    await message.reply("❌ Антивидео выключен.")


@router.message(lambda m: m.text and m.text.lower().startswith(("+антиголос", ".антиголос", "!антиголос", "+антиголосовые", ".антиголосовые")))
async def cmd_antivoice_on(message: types.Message):
    if not await is_moderator(message, 2):
        return
    chat_filters[message.chat.id]["antivoice"] = True
    await message.reply("✅ Антиголосовые включены.")


@router.message(lambda m: m.text and m.text.lower().startswith(("-антиголос", ".антиголос выкл", "!антиголос выкл", "-антиголосовые")))
async def cmd_antivoice_off(message: types.Message):
    if not await is_moderator(message, 2):
        return
    chat_filters[message.chat.id]["antivoice"] = False
    await message.reply("❌ Антиголосовые выключены.")


@router.message(lambda m: m.text and m.text.lower().startswith(("+антигиф", ".антигиф", "!антигиф", "+антигифки")))
async def cmd_antigif_on(message: types.Message):
    if not await is_moderator(message, 2):
        return
    chat_filters[message.chat.id]["antigif"] = True
    await message.reply("✅ Антигиф включён.")


@router.message(lambda m: m.text and m.text.lower().startswith(("-антигиф", ".антигиф выкл", "!антигиф выкл")))
async def cmd_antigif_off(message: types.Message):
    if not await is_moderator(message, 2):
        return
    chat_filters[message.chat.id]["antigif"] = False
    await message.reply("❌ Антигиф выключен.")


@router.message(lambda m: m.text and m.text.lower().startswith(("+антидок", ".антидок", "!антидок", "+антидокумент", ".антидокумент")))
async def cmd_antidoc_on(message: types.Message):
    if not await is_moderator(message, 2):
        return
    chat_filters[message.chat.id]["antidocument"] = True
    await message.reply("✅ Антидокументы включены.")


@router.message(lambda m: m.text and m.text.lower().startswith(("-антидок", ".антидок выкл", "!антидок выкл")))
async def cmd_antidoc_off(message: types.Message):
    if not await is_moderator(message, 2):
        return
    chat_filters[message.chat.id]["antidocument"] = False
    await message.reply("❌ Антидокументы выключены.")


# ==================== АВТОМАТИЧЕСКИЕ ФИЛЬТРЫ ====================
# PATCH: earlier catch-all decorator removed so commands and final filters can run.
async def auto_filters(message: types.Message):
    """Проверка всех сообщений на все фильтры."""
    if message.from_user.id == OWNER_ID:
        return
    if await has_rank(message, 1):
        return

    chat_id = message.chat.id
    filters = chat_filters[chat_id]

    # ===== АНТИБОТ =====
    if filters["antibot"] and message.from_user.is_bot:
        try:
            await message.bot.ban_chat_member(chat_id, message.from_user.id)
            await message.answer(f"🤖 Бот {message.from_user.mention_html()} забанен.")
        except Exception:
            pass
        return

    # ===== МЕДИА-ФИЛЬТРЫ =====
    if filters["antisticker"] and message.sticker:
        await apply_filter_punishment(message, "стикер")
        return
    if filters["antigif"] and message.animation:
        await apply_filter_punishment(message, "GIF")
        return
    if filters["antiphoto"] and message.photo:
        await apply_filter_punishment(message, "фото")
        return
    if filters["antivideo"] and message.video:
        await apply_filter_punishment(message, "видео")
        return
    if filters["antivoice"] and message.voice:
        await apply_filter_punishment(message, "голосовое")
        return
    if filters["antidocument"] and message.document:
        await apply_filter_punishment(message, "документ")
        return

    # ===== ТЕКСТОВЫЕ ФИЛЬТРЫ =====
    if not message.text:
        return

    text = message.text
    lower_text = text.lower()

    # ===== АНТИ @ALL =====
    if filters["antiall"] and ALL_PATTERN.search(text):
        await apply_filter_punishment(message, "@all")
        return

    # ===== АНТИССЫЛКИ =====
    if filters["antilink"] and URL_PATTERN.search(text):
        await apply_filter_punishment(message, "ссылка")
        return

    # ===== АНТИ-ИНВАЙТ =====
    if filters["antiinvite"] and ("t.me/joinchat" in lower_text or "telegram.me/joinchat" in lower_text):
        await apply_filter_punishment(message, "инвайт")
        return

    # ===== АНТИМАТ =====
    if filters["antimat"]:
        for word in BAD_WORDS:
            if word in lower_text:
                await apply_filter_punishment(message, "мат")
                return

    # ===== АНТИРЕКЛАМА =====
    if filters["antireklama"]:
        for word in REKLAMA_WORDS:
            if word in lower_text:
                await apply_filter_punishment(message, "реклама")
                return

    # ===== АНТИКРИПТА =====
    if filters["antikripta"]:
        for word in KRIPTA_WORDS:
            if word in lower_text:
                await apply_filter_punishment(message, "крипта")
                return

    # ===== АНТИФИШИНГ =====
    if filters["antifishing"]:
        for pattern in FISHING_PATTERNS:
            if re.search(pattern, lower_text):
                await apply_filter_punishment(message, "фишинг")
                return

    # ===== АНТИКАПС =====
    if filters["anticaps"] and len(text) > 5:
        letters = [c for c in text if c.isalpha()]
        if letters:
            caps = sum(1 for c in letters if c.isupper())
            percent = (caps / len(letters)) * 100
            if percent >= filters["anticaps_percent"]:
                await apply_filter_punishment(message, "капс")
                return

    # ===== АНТИФЛУД =====
    if filters["antiflood"]:
        now = datetime.now()
        window = filters["antiflood_window"]
        limit = filters["antiflood_limit"]

        flood_tracker[chat_id][message.from_user.id] = [
            t for t in flood_tracker[chat_id][message.from_user.id]
            if (now - t).total_seconds() < window
        ]
        flood_tracker[chat_id][message.from_user.id].append(now)

        if len(flood_tracker[chat_id][message.from_user.id]) > limit:
            try:
                await message.bot.restrict_chat_member(
                    chat_id=chat_id,
                    user_id=message.from_user.id,
                    permissions=ChatPermissions(can_send_messages=False),
                    until_date=now + timedelta(minutes=5),
                )
                await message.answer(
                    f"🌊 {message.from_user.mention_html()} замучен на 5 минут за флуд."
                )
                flood_tracker[chat_id][message.from_user.id] = []
            except Exception:
                pass
                """
Модуль модерации. Этап 4: триггеры и автонаказания.
"""

import re
from datetime import datetime, timedelta
from collections import defaultdict
from aiogram import Router, types
from aiogram.types import ChatPermissions
from database.crud import (
    get_user, create_user, add_warn, reset_warns, get_chat, set_rank,
)
from middlewares.check_rank import has_rank, get_user_rank, OWNER_ID

# PATCH: router reset removed; all handlers share the first router.

RANK_NAMES = {
    0: "Участник",
    1: "Младший модератор",
    2: "Старший модератор",
    3: "Младший администратор",
    4: "Старший администратор",
    5: "Создатель",
}

FULL_PERMISSIONS = ChatPermissions(
    can_send_messages=True,
    can_send_media_messages=True,
    can_send_other_messages=True,
    can_add_web_page_previews=True,
    can_send_polls=True,
    can_change_info=False,
    can_invite_users=True,
    can_pin_messages=False,
)

# ==================== ХРАНИЛИЩЕ ФИЛЬТРОВ ====================
chat_filters = defaultdict(lambda: {
    "antilink": False,
    "antimat": False,
    "antiflood": False,
    "antiflood_limit": 5,
    "antiflood_window": 10,
    "anticaps": False,
    "anticaps_percent": 70,
    "antisticker": False,
    "antigif": False,
    "antiphoto": False,
    "antivideo": False,
    "antivoice": False,
    "antidocument": False,
    "antibot": False,
    "antireklama": False,
    "antikripta": False,
    "antifishing": False,
    "antiinvite": False,
    "antiall": False,
    "filter_mute_time": 10,
})

# ==================== ХРАНИЛИЩЕ ТРИГГЕРОВ ====================
chat_triggers = defaultdict(lambda: {
    # Триггеры на варны (при достижении X варнов → Y наказание)
    "warn_3": {"action": "mute", "time": "1ч", "enabled": True},
    "warn_5": {"action": "mute", "time": "24ч", "enabled": True},
    "warn_7": {"action": "ban", "time": "7д", "enabled": True},
    "warn_10": {"action": "ban", "time": "0", "enabled": True},  # 0 = навсегда

    # Триггеры на ссылки
    "link_action": "ban",     # ban / mute / kick / warn
    "link_time": "30д",       # срок при ссылке
    "link_enabled": False,

    # Триггеры на мат
    "mat_action": "mute",
    "mat_time": "30м",
    "mat_enabled": False,

    # Лимит варнов
    "warn_limit": 3,
})

flood_tracker = defaultdict(lambda: defaultdict(list))

# ==================== СПИСКИ СЛОВ ====================
BAD_WORDS = [
    "хуй", "пизд", "бляд", "ебал", "ебат", "сука", "блядь", "мраз",
    "fuck", "shit", "bitch", "asshole", "dick", "pussy",
]

REKLAMA_WORDS = [
    "купить", "продам", "продаю", "скидка", "акция", "распродажа",
    "заработок", "заработать", "инвестиции", "вложить", "крипта",
    "btc", "bitcoin", "eth", "ethereum", "usdt", "toncoin", "ton",
    "казино", "ставки", "букмекер", "1xbet", "betting",
    "подпишись", "подписка", "канал", "чат", "переходи",
    "промокод", "реферал", "рефка", "приглашаю",
]

KRIPTA_WORDS = [
    "btc", "bitcoin", "eth", "ethereum", "usdt", "toncoin", "ton",
    "crypto", "крипта", "кошелёк", "wallet", "binance", "bybit",
    "p2p", "обменник", "майнинг", "mining", "нфт", "nft",
]

FISHING_PATTERNS = [
    r"t\.me/joinchat", r"telegram\.me/joinchat",
    r"bit\.ly", r"tinyurl", r"goo\.gl", r"shorturl",
    r"free.*ton", r"bonus.*ton", r"gift.*ton",
    r"airdrop", r"giveaway", r"claim.*reward",
    r"verify.*wallet", r"connect.*wallet", r"seed.*phrase",
]

URL_PATTERN = re.compile(r"(https?://|www\.|t\.me/|telegram\.me/)", re.IGNORECASE)
ALL_PATTERN = re.compile(r"@(all|everyone|here)", re.IGNORECASE)


# ==================== ПАРСЕР ВРЕМЕНИ ====================
def parse_time(text: str):
    if not text:
        return None
    text = text.lower().strip()

    time_units = {
        "сек": 1, "секунда": 1, "секунды": 1, "секунд": 1, "с": 1,
        "мин": 60, "минута": 60, "минуты": 60, "минут": 60, "м": 60,
        "час": 3600, "часа": 3600, "часов": 3600, "ч": 3600,
        "день": 86400, "дня": 86400, "дней": 86400, "д": 86400, "сутки": 86400,
        "нед": 604800, "неделя": 604800, "недели": 604800, "недель": 604800, "н": 604800,
        "мес": 2592000, "месяц": 2592000, "месяца": 2592000, "месяцев": 2592000,
        "год": 31536000, "года": 31536000, "лет": 31536000, "г": 31536000,
    }

    match = re.match(r"^(\d+)\s*([а-яa-z]+)$", text)
    if match:
        num = int(match.group(1))
        unit = match.group(2)
        for key, multiplier in time_units.items():
            if unit.startswith(key) or key.startswith(unit):
                return timedelta(seconds=num * multiplier)
        return None

    match = re.match(r"^(\d+)", text)
    if not match:
        return None

    num = int(match.group(1))
    rest = text[match.end():].strip()
    for unit, multiplier in time_units.items():
        if unit in rest:
            return timedelta(seconds=num * multiplier)

    return None


async def is_moderator(message: types.Message, required: int = 1) -> bool:
    if message.from_user.id == OWNER_ID:
        return True
    return await has_rank(message, required)


async def get_target(message: types.Message):
    if message.reply_to_message:
        return message.reply_to_message.from_user

    args = message.text.split()
    if len(args) < 2:
        return None

    target_username = args[1].lstrip("@")
    try:
        member = await message.chat.get_member(target_username)
        return member.user
    except Exception:
        return None


def get_duration_arg(message: types.Message):
    if not message.text:
        return None
    args = message.text.split()
    start_index = 1
    if len(args) > 1 and args[1].startswith("@"):
        start_index = 2
    time_parts = args[start_index:]
    if not time_parts:
        return None
    time_str = " ".join(time_parts)
    if not re.search(r"\d", time_str):
        return None
    return time_str


async def apply_filter_punishment(message: types.Message, reason: str):
    chat_id = message.chat.id
    mute_time = chat_filters[chat_id]["filter_mute_time"]
    try:
        await message.delete()
    except Exception:
        pass
    try:
        await message.bot.restrict_chat_member(
            chat_id=chat_id,
            user_id=message.from_user.id,
            permissions=ChatPermissions(can_send_messages=False),
            until_date=datetime.now() + timedelta(minutes=mute_time),
        )
        await message.answer(
            f"🚫 {message.from_user.mention_html()} замучен на {mute_time} мин. "
            f"Причина: {reason}"
        )
    except Exception:
        pass


async def apply_punishment(bot, chat_id: int, user_id: int, action: str, time_str: str, reason: str):
    """Универсальная функция наказания."""
    try:
        if action == "mute":
            duration = parse_time(time_str) if time_str else None
            until = datetime.now() + duration if duration else datetime.now() + timedelta(days=3650)
            await bot.restrict_chat_member(
                chat_id=chat_id,
                user_id=user_id,
                permissions=ChatPermissions(can_send_messages=False),
                until_date=until,
            )
            await bot.send_message(
                chat_id,
                f"🔇 <a href='tg://user?id={user_id}'>Пользователь</a> замучен на {time_str or 'бессрочно'}. Причина: {reason}"
            )
        elif action == "ban":
            await bot.ban_chat_member(chat_id, user_id)
            await bot.send_message(
                chat_id,
                f"🔨 <a href='tg://user?id={user_id}'>Пользователь</a> забанен на {time_str or 'навсегда'}. Причина: {reason}"
            )
        elif action == "kick":
            await bot.ban_chat_member(chat_id, user_id)
            await bot.unban_chat_member(chat_id, user_id)
            await bot.send_message(
                chat_id,
                f"👢 <a href='tg://user?id={user_id}'>Пользователь</a> кикнут. Причина: {reason}"
            )
        elif action == "warn":
            warns = add_warn(user_id, chat_id)
            await bot.send_message(
                chat_id,
                f"⚠️ <a href='tg://user?id={user_id}'>Пользователь</a> получил варн. Всего: {warns}. Причина: {reason}"
            )
    except Exception as e:
        print(f"Punishment error: {e}")


# ==================== БАЗОВЫЕ КОМАНДЫ ====================
@router.message(lambda m: m.text and m.text.lower().startswith((
    "мут", ".мут", "!мут", "/мут", "mute", ".mute", "!mute", "/mute"
)))
async def cmd_mute(message: types.Message):
    if not await is_moderator(message, 1):
        await message.reply("❌ Недостаточно прав.")
        return

    target = await get_target(message)
    if not target:
        await message.reply("❌ Ответьте на сообщение или укажите @username.")
        return
    if target.id == OWNER_ID:
        await message.reply("❌ Нельзя замутить владельца.")
        return
    if target.id == message.from_user.id:
        await message.reply("❌ Нельзя замутить самого себя.")
        return

    duration, time_text, reason = parse_duration_reason(message)
    until = datetime.now() + duration if duration else datetime.now() + timedelta(days=3650)

    try:
        await message.bot.restrict_chat_member(
            chat_id=message.chat.id,
            user_id=target.id,
            permissions=ChatPermissions(can_send_messages=False),
            until_date=until,
        )
        try:
            log_action(message.chat.id, "mutes", message.from_user.id, target.id, time=time_text, reason=reason)
        except Exception:
            pass
        await message.reply(f"🔇 {target.mention_html()} замучен на {time_text}. Причина: {reason}.")
    except Exception as e:
        await message.reply(f"❌ Ошибка: {e}")


@router.message(lambda m: m.text and m.text.lower().startswith((
    "размут", ".размут", "!размут", "/размут",
    "унмут", ".унмут", "!унмут", "/унмут",
    "анмут", ".анмут", "!анмут", "/анмут",
    "unmute", ".unmute", "!unmute", "/unmute"
)))
async def cmd_unmute(message: types.Message):
    if not await is_moderator(message, 1):
        await message.reply("❌ Недостаточно прав.")
        return

    target = await get_target(message)
    if not target:
        await message.reply("❌ Ответьте на сообщение или укажите @username.")
        return

    try:
        await message.bot.restrict_chat_member(
            chat_id=message.chat.id,
            user_id=target.id,
            permissions=FULL_PERMISSIONS,
        )
        await message.reply(f"🔊 {target.mention_html()} размучен.")
    except Exception as e:
        await message.reply(f"❌ Ошибка: {e}")


@router.message(lambda m: m.text and m.text.lower().startswith((
    "бан", ".бан", "!бан", "/бан", "ban", ".ban", "!ban", "/ban"
)))
async def cmd_ban(message: types.Message):
    if not await is_moderator(message, 2):
        await message.reply("❌ Недостаточно прав.")
        return

    target = await get_target(message)
    if not target:
        await message.reply("❌ Ответьте на сообщение или укажите @username.")
        return
    if target.id == OWNER_ID:
        await message.reply("❌ Нельзя забанить владельца.")
        return

    time_arg = get_duration_arg(message)
    duration = parse_time(time_arg) if time_arg else None

    try:
        await message.bot.ban_chat_member(message.chat.id, target.id)
        if duration:
            await message.reply(f"🔨 {target.mention_html()} забанен на {time_arg}.")
        else:
            await message.reply(f"🔨 {target.mention_html()} забанен навсегда.")
    except Exception as e:
        await message.reply(f"❌ Ошибка: {e}")


@router.message(lambda m: m.text and m.text.lower().startswith((
    "разбан", ".разбан", "!разбан", "/разбан",
    "унбан", ".унбан", "!унбан", "/унбан",
    "unban", ".unban", "!unban", "/unban"
)))
async def cmd_unban(message: types.Message):
    if not await is_moderator(message, 2):
        await message.reply("❌ Недостаточно прав.")
        return

    target = await get_target(message)
    if not target:
        await message.reply("❌ Ответьте на сообщение или укажите @username.")
        return

    try:
        await message.bot.unban_chat_member(message.chat.id, target.id)
        await message.reply(f"✅ {target.mention_html()} разбанен.")
    except Exception as e:
        await message.reply(f"❌ Ошибка: {e}")


@router.message(lambda m: m.text and m.text.lower().startswith((
    "кик", ".кик", "!кик", "/кик", "kick", ".kick", "!kick", "/kick"
)))
async def cmd_kick(message: types.Message):
    if not await is_moderator(message, 1):
        await message.reply("❌ Недостаточно прав.")
        return

    target = await get_target(message)
    if not target:
        await message.reply("❌ Ответьте на сообщение или укажите @username.")
        return
    if target.id == OWNER_ID:
        await message.reply("❌ Нельзя кикнуть владельца.")
        return

    try:
        await message.bot.ban_chat_member(message.chat.id, target.id)
        await message.bot.unban_chat_member(message.chat.id, target.id)
        await message.reply(f"👢 {target.mention_html()} кикнут.")
    except Exception as e:
        await message.reply(f"❌ Ошибка: {e}")


@router.message(lambda m: m.text and m.text.lower().startswith((
    "варн", ".варн", "!варн", "/варн",
    "пред", ".пред", "!пред", "/пред",
    "warn", ".warn", "!warn", "/warn"
)))
async def cmd_warn(message: types.Message):
    if not await is_moderator(message, 1):
        await message.reply("❌ Недостаточно прав.")
        return

    target = await get_target(message)
    if not target:
        await message.reply("❌ Ответьте на сообщение или укажите @username.")
        return
    if target.id == OWNER_ID:
        await message.reply("❌ Нельзя выдать варн владельцу.")
        return

    warns = add_warn(target.id, message.chat.id)
    if warns is None:
        create_user(target.id, message.chat.id, target.username, target.first_name)
        warns = add_warn(target.id, message.chat.id)

    await message.reply(f"⚠️ {target.mention_html()} получил варн. Всего: {warns}")

    # ===== ПРОВЕРКА ТРИГГЕРОВ НА ВАРНЫ =====
    triggers = chat_triggers[message.chat.id]
    for trigger_key in ["warn_3", "warn_5", "warn_7", "warn_10"]:
        trigger = triggers.get(trigger_key)
        if not trigger or not trigger.get("enabled"):
            continue
        threshold = int(trigger_key.split("_")[1])
        if warns == threshold:
            await apply_punishment(
                message.bot,
                message.chat.id,
                target.id,
                trigger["action"],
                trigger["time"],
                f"достигнут лимит варнов ({threshold})",
            )
            reset_warns(target.id, message.chat.id)
            break


@router.message(lambda m: m.text and m.text.lower().startswith((
    "унварн", ".унварн", "!унварн", "/унварн",
    "анварн", ".анварн", "!анварн", "/анварн",
    "снятьварн", ".снятьварн", "!снятьварн", "/снятьварн",
    "unwarn", ".unwarn", "!unwarn", "/unwarn"
)))
async def cmd_unwarn(message: types.Message):
    if not await is_moderator(message, 2):
        await message.reply("❌ Недостаточно прав.")
        return

    target = await get_target(message)
    if not target:
        await message.reply("❌ Ответьте на сообщение или укажите @username.")
        return

    reset_warns(target.id, message.chat.id)
    await message.reply(f"✅ Варны {target.mention_html()} сняты.")


@router.message(lambda m: m.text and m.text.lower().startswith((
    "варны", ".варны", "!варны", "/варны",
    "преды", ".преды", "!преды", "/преды",
    "warns", ".warns", "!warns", "/warns"
)))
async def cmd_warns(message: types.Message):
    target = await get_target(message)
    if not target:
        target = message.from_user

    user = get_user(target.id, message.chat.id)
    if not user:
        await message.reply(f"У {target.mention_html()} нет предупреждений.")
        return

    await message.reply(f"⚠️ {target.mention_html()}: <b>{user['warns']}</b> предупреждений.")


@router.message(lambda m: m.text and m.text.lower().startswith((
    "повысить", ".повысить", "!повысить", "/повысить",
    "promote", ".promote", "!promote", "/promote"
)))
async def cmd_promote(message: types.Message):
    if not await is_moderator(message, 3):
        await message.reply("❌ Недостаточно прав.")
        return

    target = await get_target(message)
    if not target:
        await message.reply("❌ Ответьте на сообщение или укажите @username.")
        return

    current = await get_user_rank(target.id, message.chat.id)
    if current >= 5:
        await message.reply("Максимальный ранг.")
        return

    set_rank(target.id, message.chat.id, current + 1)
    await message.reply(f"⬆️ {target.mention_html()} повышен до <b>{RANK_NAMES[current + 1]}</b>")


@router.message(lambda m: m.text and m.text.lower().startswith((
    "понизить", ".понизить", "!понизить", "/понизить",
    "demote", ".demote", "!demote", "/demote"
)))
async def cmd_demote(message: types.Message):
    if not await is_moderator(message, 4):
        await message.reply("❌ Недостаточно прав.")
        return

    target = await get_target(message)
    if not target:
        await message.reply("❌ Ответьте на сообщение или укажите @username.")
        return

    current = await get_user_rank(target.id, message.chat.id)
    if current <= 0:
        return

    set_rank(target.id, message.chat.id, current - 1)
    await message.reply(f"⬇️ {target.mention_html()} понижен до <b>{RANK_NAMES[current - 1]}</b>")


@router.message(lambda m: m.text and m.text.lower().startswith((
    "мойранг", ".мойранг", "!мойранг", "/мойранг",
    "rank", ".rank", "!rank", "/rank",
    "ктоя", ".ктоя", "!ктоя", "/ктоя"
)))
async def cmd_rank(message: types.Message):
    target = message.reply_to_message.from_user if message.reply_to_message else message.from_user
    rank = await get_user_rank(target.id, message.chat.id)
    await message.reply(f"Ранг {target.mention_html()}: <b>{RANK_NAMES.get(rank, 'Участник')}</b>")


# ==================== НАСТРОЙКА ТРИГГЕРОВ ====================
@router.message(lambda m: m.text and m.text.lower().startswith((
    "триггер варн", ".триггер варн", "!триггер варн"
)))
async def cmd_set_warn_trigger(message: types.Message):
    """Настройка триггера: триггер варн 3 мут 1ч"""
    if not await is_moderator(message, 3):
        await message.reply("❌ Недостаточно прав.")
        return

    args = message.text.split()
    # формат: триггер варн <N> <action> <time>
    if len(args) < 5:
        await message.reply(
            "Формат: <code>триггер варн 3 мут 1ч</code>\n"
            "Действия: мут, бан, кик, варн"
        )
        return

    try:
        threshold = int(args[2])
    except ValueError:
        await message.reply("❌ Число варнов должно быть числом.")
        return

    action_map = {"мут": "mute", "бан": "ban", "кик": "kick", "варн": "warn"}
    action = action_map.get(args[3].lower())
    if not action:
        await message.reply("❌ Действие: мут, бан, кик, варн")
        return

    time_str = args[4] if len(args) > 4 else "0"

    key = f"warn_{threshold}"
    chat_triggers[message.chat.id][key] = {
        "action": action,
        "time": time_str,
        "enabled": True,
    }
    await message.reply(
        f"✅ Триггер: <b>{threshold}</b> варнов → <b>{args[3]}</b> на <b>{time_str}</b>"
    )


@router.message(lambda m: m.text and m.text.lower().startswith((
    "триггер ссылка", ".триггер ссылка", "!триггер ссылка"
)))
async def cmd_set_link_trigger(message: types.Message):
    """Настройка триггера на ссылки: триггер ссылка бан 30д"""
    if not await is_moderator(message, 3):
        await message.reply("❌ Недостаточно прав.")
        return

    args = message.text.split()
    if len(args) < 4:
        await message.reply(
            "Формат: <code>триггер ссылка бан 30д</code>\n"
            "Действия: мут, бан, кик, варн"
        )
        return

    action_map = {"мут": "mute", "бан": "ban", "кик": "kick", "варн": "warn"}
    action = action_map.get(args[2].lower())
    if not action:
        await message.reply("❌ Действие: мут, бан, кик, варн")
        return

    time_str = args[3] if len(args) > 3 else "0"

    chat_triggers[message.chat.id]["link_action"] = action
    chat_triggers[message.chat.id]["link_time"] = time_str
    chat_triggers[message.chat.id]["link_enabled"] = True
    await message.reply(
        f"✅ Триггер на ссылки: <b>{args[2]}</b> на <b>{time_str}</b>"
    )


@router.message(lambda m: m.text and m.text.lower().startswith((
    "триггер мат", ".триггер мат", "!триггер мат"
)))
async def cmd_set_mat_trigger(message: types.Message):
    """Настройка триггера на мат: триггер мат мут 30м"""
    if not await is_moderator(message, 3):
        await message.reply("❌ Недостаточно прав.")
        return

    args = message.text.split()
    if len(args) < 4:
        await message.reply(
            "Формат: <code>триггер мат мут 30м</code>\n"
            "Действия: мут, бан, кик, варн"
        )
        return

    action_map = {"мут": "mute", "бан": "ban", "кик": "kick", "варн": "warn"}
    action = action_map.get(args[2].lower())
    if not action:
        await message.reply("❌ Действие: мут, бан, кик, варн")
        return

    time_str = args[3] if len(args) > 3 else "0"

    chat_triggers[message.chat.id]["mat_action"] = action
    chat_triggers[message.chat.id]["mat_time"] = time_str
    chat_triggers[message.chat.id]["mat_enabled"] = True
    await message.reply(
        f"✅ Триггер на мат: <b>{args[2]}</b> на <b>{time_str}</b>"
    )


@router.message(lambda m: m.text and m.text.lower().startswith((
    "триггеры", ".триггеры", "!триггеры"
)))
async def cmd_list_triggers(message: types.Message):
    """Показать все триггеры чата."""
    triggers = chat_triggers[message.chat.id]
    text = "<b>📋 Триггеры чата:</b>\n\n"

    for key in ["warn_3", "warn_5", "warn_7", "warn_10"]:
        t = triggers.get(key)
        if t and t.get("enabled"):
            threshold = key.split("_")[1]
            text += f"• {threshold} варнов → {t['action']} на {t['time']}\n"

    if triggers["link_enabled"]:
        text += f"• Ссылка → {triggers['link_action']} на {triggers['link_time']}\n"

    if triggers["mat_enabled"]:
        text += f"• Мат → {triggers['mat_action']} на {triggers['mat_time']}\n"

    await message.reply(text)


# ==================== УПРАВЛЕНИЕ ФИЛЬТРАМИ ====================
@router.message(lambda m: m.text and m.text.lower().startswith(("+антиссылка", ".антиссылка", "!антиссылка")))
async def cmd_antilink_on(message: types.Message):
    if not await is_moderator(message, 2):
        return
    chat_filters[message.chat.id]["antilink"] = True
    await message.reply("✅ Антиссылки включены.")


@router.message(lambda m: m.text and m.text.lower().startswith(("-антиссылка", ".антиссылка выкл", "!антиссылка выкл")))
async def cmd_antilink_off(message: types.Message):
    if not await is_moderator(message, 2):
        return
    chat_filters[message.chat.id]["antilink"] = False
    await message.reply("❌ Антиссылки выключены.")


@router.message(lambda m: m.text and m.text.lower().startswith(("+антимат", ".антимат", "!антимат")))
async def cmd_antimat_on(message: types.Message):
    if not await is_moderator(message, 2):
        return
    chat_filters[message.chat.id]["antimat"] = True
    await message.reply("✅ Антимат включён.")


@router.message(lambda m: m.text and m.text.lower().startswith(("-антимат", ".антимат выкл", "!антимат выкл")))
async def cmd_antimat_off(message: types.Message):
    if not await is_moderator(message, 2):
        return
    chat_filters[message.chat.id]["antimat"] = False
    await message.reply("❌ Антимат выключен.")


@router.message(lambda m: m.text and m.text.lower().startswith(("+антифлуд", ".антифлуд", "!антифлуд")))
async def cmd_antiflood_on(message: types.Message):
    if not await is_moderator(message, 2):
        return
    chat_filters[message.chat.id]["antiflood"] = True
    await message.reply("✅ Антифлуд включён.")


@router.message(lambda m: m.text and m.text.lower().startswith(("-антифлуд", ".антифлуд выкл", "!антифлуд выкл")))
async def cmd_antiflood_off(message: types.Message):
    if not await is_moderator(message, 2):
        return
    chat_filters[message.chat.id]["antiflood"] = False
    await message.reply("❌ Антифлуд выключен.")


@router.message(lambda m: m.text and m.text.lower().startswith(("+антикапс", ".антикапс", "!антикапс")))
async def cmd_anticaps_on(message: types.Message):
    if not await is_moderator(message, 2):
        return
    chat_filters[message.chat.id]["anticaps"] = True
    await message.reply("✅ Антикапс включён.")


@router.message(lambda m: m.text and m.text.lower().startswith(("-антикапс", ".антикапс выкл", "!антикапс выкл")))
async def cmd_anticaps_off(message: types.Message):
    if not await is_moderator(message, 2):
        return
    chat_filters[message.chat.id]["anticaps"] = False
    await message.reply("❌ Антикапс выключен.")


@router.message(lambda m: m.text and m.text.lower().startswith(("+антибот", ".антибот", "!антибот")))
async def cmd_antibot_on(message: types.Message):
    if not await is_moderator(message, 2):
        return
    chat_filters[message.chat.id]["antibot"] = True
    await message.reply("✅ Антибот включён.")


@router.message(lambda m: m.text and m.text.lower().startswith(("-антибот", ".антибот выкл", "!антибот выкл")))
async def cmd_antibot_off(message: types.Message):
    if not await is_moderator(message, 2):
        return
    chat_filters[message.chat.id]["antibot"] = False
    await message.reply("❌ Антибот выключен.")


@router.message(lambda m: m.text and m.text.lower().startswith(("+антиреклама", ".антиреклама", "!антиреклама")))
async def cmd_antireklama_on(message: types.Message):
    if not await is_moderator(message, 2):
        return
    chat_filters[message.chat.id]["antireklama"] = True
    await message.reply("✅ Антиреклама включена.")


@router.message(lambda m: m.text and m.text.lower().startswith(("-антиреклама", ".антиреклама выкл", "!антиреклама выкл")))
async def cmd_antireklama_off(message: types.Message):
    if not await is_moderator(message, 2):
        return
    chat_filters[message.chat.id]["antireklama"] = False
    await message.reply("❌ Антиреклама выключена.")


@router.message(lambda m: m.text and m.text.lower().startswith(("+антикрипта", ".антикрипта", "!антикрипта")))
async def cmd_antikripta_on(message: types.Message):
    if not await is_moderator(message, 2):
        return
    chat_filters[message.chat.id]["antikripta"] = True
    await message.reply("✅ Антикрипта включена.")


@router.message(lambda m: m.text and m.text.lower().startswith(("-антикрипта", ".антикрипта выкл", "!антикрипта выкл")))
async def cmd_antikripta_off(message: types.Message):
    if not await is_moderator(message, 2):
        return
    chat_filters[message.chat.id]["antikripta"] = False
    await message.reply("❌ Антикрипта выключена.")


@router.message(lambda m: m.text and m.text.lower().startswith(("+антифишинг", ".антифишинг", "!антифишинг")))
async def cmd_antifishing_on(message: types.Message):
    if not await is_moderator(message, 2):
        return
    chat_filters[message.chat.id]["antifishing"] = True
    await message.reply("✅ Антифишинг включён.")


@router.message(lambda m: m.text and m.text.lower().startswith(("-антифишинг", ".антифишинг выкл", "!антифишинг выкл")))
async def cmd_antifishing_off(message: types.Message):
    if not await is_moderator(message, 2):
        return
    chat_filters[message.chat.id]["antifishing"] = False
    await message.reply("❌ Антифишинг выключен.")


@router.message(lambda m: m.text and m.text.lower().startswith(("+антивызов", ".антивызов", "!антивызов", "+антиинвайт", ".антиинвайт")))
async def cmd_antiinvite_on(message: types.Message):
    if not await is_moderator(message, 2):
        return
    chat_filters[message.chat.id]["antiinvite"] = True
    await message.reply("✅ Анти-инвайт включён.")


@router.message(lambda m: m.text and m.text.lower().startswith(("-антиинвайт", ".антиинвайт выкл", "!антиинвайт выкл")))
async def cmd_antiinvite_off(message: types.Message):
    if not await is_moderator(message, 2):
        return
    chat_filters[message.chat.id]["antiinvite"] = False
    await message.reply("❌ Анти-инвайт выключен.")


@router.message(lambda m: m.text and m.text.lower().startswith(("+антиолл", ".антиолл", "!антиолл", "+антиалл", ".антиалл")))
async def cmd_antiall_on(message: types.Message):
    if not await is_moderator(message, 2):
        return
    chat_filters[message.chat.id]["antiall"] = True
    await message.reply("✅ Анти @all включён.")


@router.message(lambda m: m.text and m.text.lower().startswith(("-антиолл", ".антиолл выкл", "!антиолл выкл", "-антиалл")))
async def cmd_antiall_off(message: types.Message):
    if not await is_moderator(message, 2):
        return
    chat_filters[message.chat.id]["antiall"] = False
    await message.reply("❌ Анти @all выключен.")


@router.message(lambda m: m.text and m.text.lower().startswith(("+антифото", ".антифото", "!антифото")))
async def cmd_antiphoto_on(message: types.Message):
    if not await is_moderator(message, 2):
        return
    chat_filters[message.chat.id]["antiphoto"] = True
    await message.reply("✅ Антифото включён.")


@router.message(lambda m: m.text and m.text.lower().startswith(("-антифото", ".антифото выкл", "!антифото выкл")))
async def cmd_antiphoto_off(message: types.Message):
    if not await is_moderator(message, 2):
        return
    chat_filters[message.chat.id]["antiphoto"] = False
    await message.reply("❌ Антифото выключен.")


@router.message(lambda m: m.text and m.text.lower().startswith(("+антивидео", ".антивидео", "!антивидео")))
async def cmd_antivideo_on(message: types.Message):
    if not await is_moderator(message, 2):
        return
    chat_filters[message.chat.id]["antivideo"] = True
    await message.reply("✅ Антивидео включён.")


@router.message(lambda m: m.text and m.text.lower().startswith(("-антивидео", ".антивидео выкл", "!антивидео выкл")))
async def cmd_antivideo_off(message: types.Message):
    if not await is_moderator(message, 2):
        return
    chat_filters[message.chat.id]["antivideo"] = False
    await message.reply("❌ Антивидео выключен.")


@router.message(lambda m: m.text and m.text.lower().startswith(("+антиголос", ".антиголос", "!антиголос")))
async def cmd_antivoice_on(message: types.Message):
    if not await is_moderator(message, 2):
        return
    chat_filters[message.chat.id]["antivoice"] = True
    await message.reply("✅ Антиголосовые включены.")


@router.message(lambda m: m.text and m.text.lower().startswith(("-антиголос", ".антиголос выкл", "!антиголос выкл")))
async def cmd_antivoice_off(message: types.Message):
    if not await is_moderator(message, 2):
        return
    chat_filters[message.chat.id]["antivoice"] = False
    await message.reply("❌ Антиголосовые выключены.")


@router.message(lambda m: m.text and m.text.lower().startswith(("+антигиф", ".антигиф", "!антигиф")))
async def cmd_antigif_on(message: types.Message):
    if not await is_moderator(message, 2):
        return
    chat_filters[message.chat.id]["antigif"] = True
    await message.reply("✅ Антигиф включён.")


@router.message(lambda m: m.text and m.text.lower().startswith(("-антигиф", ".антигиф выкл", "!антигиф выкл")))
async def cmd_antigif_off(message: types.Message):
    if not await is_moderator(message, 2):
        return
    chat_filters[message.chat.id]["antigif"] = False
    await message.reply("❌ Антигиф выключен.")


@router.message(lambda m: m.text and m.text.lower().startswith(("+антидок", ".антидок", "!антидок")))
async def cmd_antidoc_on(message: types.Message):
    if not await is_moderator(message, 2):
        return
    chat_filters[message.chat.id]["antidocument"] = True
    await message.reply("✅ Антидокументы включены.")


@router.message(lambda m: m.text and m.text.lower().startswith(("-антидок", ".антидок выкл", "!антидок выкл")))
async def cmd_antidoc_off(message: types.Message):
    if not await is_moderator(message, 2):
        return
    chat_filters[message.chat.id]["antidocument"] = False
    await message.reply("❌ Антидокументы выключены.")


# ==================== АВТОМАТИЧЕСКИЕ ФИЛЬТРЫ + ТРИГГЕРЫ ====================
# PATCH: earlier catch-all decorator removed so commands and final filters can run.
async def auto_filters(message: types.Message):
    if message.from_user.id == OWNER_ID:
        return
    if await has_rank(message, 1):
        return

    chat_id = message.chat.id
    filters = chat_filters[chat_id]
    triggers = chat_triggers[chat_id]

    # ===== АНТИБОТ =====
    if filters["antibot"] and message.from_user.is_bot:
        try:
            await message.bot.ban_chat_member(chat_id, message.from_user.id)
            await message.answer(f"🤖 Бот {message.from_user.mention_html()} забанен.")
        except Exception:
            pass
        return

    # ===== МЕДИА-ФИЛЬТРЫ =====
    if filters["antisticker"] and message.sticker:
        await apply_filter_punishment(message, "стикер")
        return
    if filters["antigif"] and message.animation:
        await apply_filter_punishment(message, "GIF")
        return
    if filters["antiphoto"] and message.photo:
        await apply_filter_punishment(message, "фото")
        return
    if filters["antivideo"] and message.video:
        await apply_filter_punishment(message, "видео")
        return
    if filters["antivoice"] and message.voice:
        await apply_filter_punishment(message, "голосовое")
        return
    if filters["antidocument"] and message.document:
        await apply_filter_punishment(message, "документ")
        return

    # ===== ТЕКСТОВЫЕ ТРИГГЕРЫ И ФИЛЬТРЫ =====
    if not message.text:
        return

    text = message.text
    lower_text = text.lower()

    # ===== ТРИГГЕР НА ССЫЛКИ =====
    if triggers["link_enabled"] and URL_PATTERN.search(text):
        await apply_punishment(
            message.bot, chat_id, message.from_user.id,
            triggers["link_action"], triggers["link_time"],
            "ссылка в чате",
        )
        return

    # ===== ТРИГГЕР НА МАТ =====
    if triggers["mat_enabled"]:
        for word in BAD_WORDS:
            if word in lower_text:
                await apply_punishment(
                    message.bot, chat_id, message.from_user.id,
                    triggers["mat_action"], triggers["mat_time"],
                    "нецензурная лексика",
                )
                return

    # ===== АНТИ @ALL =====
    if filters["antiall"] and ALL_PATTERN.search(text):
        await apply_filter_punishment(message, "@all")
        return

    # ===== АНТИССЫЛКИ =====
    if filters["antilink"] and URL_PATTERN.search(text):
        await apply_filter_punishment(message, "ссылка")
        return

    # ===== АНТИ-ИНВАЙТ =====
    if filters["antiinvite"] and ("t.me/joinchat" in lower_text or "telegram.me/joinchat" in lower_text):
        await apply_filter_punishment(message, "инвайт")
        return

    # ===== АНТИМАТ =====
    if filters["antimat"]:
        for word in BAD_WORDS:
            if word in lower_text:
                await apply_filter_punishment(message, "мат")
                return

    # ===== АНТИРЕКЛАМА =====
    if filters["antireklama"]:
        for word in REKLAMA_WORDS:
            if word in lower_text:
                await apply_filter_punishment(message, "реклама")
                return

    # ===== АНТИКРИПТА =====
    if filters["antikripta"]:
        for word in KRIPTA_WORDS:
            if word in lower_text:
                await apply_filter_punishment(message, "крипта")
                return

    # ===== АНТИФИШИНГ =====
    if filters["antifishing"]:
        for pattern in FISHING_PATTERNS:
            if re.search(pattern, lower_text):
                await apply_filter_punishment(message, "фишинг")
                return

    # ===== АНТИКАПС =====
    if filters["anticaps"] and len(text) > 5:
        letters = [c for c in text if c.isalpha()]
        if letters:
            caps = sum(1 for c in letters if c.isupper())
            percent = (caps / len(letters)) * 100
            if percent >= filters["anticaps_percent"]:
                await apply_filter_punishment(message, "капс")
                return

    # ===== АНТИФЛУД =====
    if filters["antiflood"]:
        now = datetime.now()
        window = filters["antiflood_window"]
        limit = filters["antiflood_limit"]

        flood_tracker[chat_id][message.from_user.id] = [
            t for t in flood_tracker[chat_id][message.from_user.id]
            if (now - t).total_seconds() < window
        ]
        flood_tracker[chat_id][message.from_user.id].append(now)

        if len(flood_tracker[chat_id][message.from_user.id]) > limit:
            try:
                await message.bot.restrict_chat_member(
                    chat_id=chat_id,
                    user_id=message.from_user.id,
                    permissions=ChatPermissions(can_send_messages=False),
                    until_date=now + timedelta(minutes=5),
                )
                await message.answer(
                    f"🌊 {message.from_user.mention_html()} замучен на 5 минут за флуд."
                )
                flood_tracker[chat_id][message.from_user.id] = []
            except Exception:
                pass
                """
Модуль модерации. Этап 5: управление сообщениями, очистка, информация.
"""

import re
from datetime import datetime, timedelta
from collections import defaultdict
from aiogram import Router, types
from aiogram.types import ChatPermissions
from database.crud import (
    get_user, create_user, add_warn, reset_warns, get_chat, set_rank,
)
from middlewares.check_rank import has_rank, get_user_rank, OWNER_ID

# PATCH: router reset removed; all handlers share the first router.

RANK_NAMES = {
    0: "Участник",
    1: "Младший модератор",
    2: "Старший модератор",
    3: "Младший администратор",
    4: "Старший администратор",
    5: "Создатель",
}

FULL_PERMISSIONS = ChatPermissions(
    can_send_messages=True,
    can_send_media_messages=True,
    can_send_other_messages=True,
    can_add_web_page_previews=True,
    can_send_polls=True,
    can_change_info=False,
    can_invite_users=True,
    can_pin_messages=False,
)

# ==================== ХРАНИЛИЩА ====================
chat_filters = defaultdict(lambda: {
    "antilink": False, "antimat": False,
    "antiflood": False, "antiflood_limit": 5, "antiflood_window": 10,
    "anticaps": False, "anticaps_percent": 70,
    "antisticker": False, "antigif": False, "antiphoto": False,
    "antivideo": False, "antivoice": False, "antidocument": False,
    "antibot": False, "antireklama": False, "antikripta": False,
    "antifishing": False, "antiinvite": False, "antiall": False,
    "filter_mute_time": 10,
})

chat_triggers = defaultdict(lambda: {
    "warn_3": {"action": "mute", "time": "1ч", "enabled": True},
    "warn_5": {"action": "mute", "time": "24ч", "enabled": True},
    "warn_7": {"action": "ban", "time": "7д", "enabled": True},
    "warn_10": {"action": "ban", "time": "0", "enabled": True},
    "link_action": "ban", "link_time": "30д", "link_enabled": False,
    "mat_action": "mute", "mat_time": "30м", "mat_enabled": False,
    "warn_limit": 3,
})

flood_tracker = defaultdict(lambda: defaultdict(list))

# Статистика чата (в памяти)
chat_stats = defaultdict(lambda: {
    "messages": 0,
    "users": defaultdict(int),
    "created": datetime.now(),
})

# ==================== СПИСКИ ====================
BAD_WORDS = [
    "хуй", "пизд", "бляд", "ебал", "ебат", "сука", "блядь", "мраз",
    "fuck", "shit", "bitch", "asshole", "dick", "pussy",
]

REKLAMA_WORDS = [
    "купить", "продам", "продаю", "скидка", "акция", "распродажа",
    "заработок", "заработать", "инвестиции", "вложить", "крипта",
    "btc", "bitcoin", "eth", "ethereum", "usdt", "toncoin", "ton",
    "казино", "ставки", "букмекер", "1xbet", "betting",
    "подпишись", "подписка", "канал", "чат", "переходи",
    "промокод", "реферал", "рефка", "приглашаю",
]

KRIPTA_WORDS = [
    "btc", "bitcoin", "eth", "ethereum", "usdt", "toncoin", "ton",
    "crypto", "крипта", "кошелёк", "wallet", "binance", "bybit",
    "p2p", "обменник", "майнинг", "mining", "нфт", "nft",
]

FISHING_PATTERNS = [
    r"t\.me/joinchat", r"telegram\.me/joinchat",
    r"bit\.ly", r"tinyurl", r"goo\.gl", r"shorturl",
    r"free.*ton", r"bonus.*ton", r"gift.*ton",
    r"airdrop", r"giveaway", r"claim.*reward",
    r"verify.*wallet", r"connect.*wallet", r"seed.*phrase",
]

URL_PATTERN = re.compile(r"(https?://|www\.|t\.me/|telegram\.me/)", re.IGNORECASE)
ALL_PATTERN = re.compile(r"@(all|everyone|here)", re.IGNORECASE)


# ==================== ПАРСЕР ВРЕМЕНИ ====================
def parse_time(text: str):
    if not text:
        return None
    text = text.lower().strip()

    time_units = {
        "сек": 1, "секунда": 1, "секунды": 1, "секунд": 1, "с": 1,
        "мин": 60, "минута": 60, "минуты": 60, "минут": 60, "м": 60,
        "час": 3600, "часа": 3600, "часов": 3600, "ч": 3600,
        "день": 86400, "дня": 86400, "дней": 86400, "д": 86400, "сутки": 86400,
        "нед": 604800, "неделя": 604800, "недели": 604800, "недель": 604800, "н": 604800,
        "мес": 2592000, "месяц": 2592000, "месяца": 2592000, "месяцев": 2592000,
        "год": 31536000, "года": 31536000, "лет": 31536000, "г": 31536000,
    }

    match = re.match(r"^(\d+)\s*([а-яa-z]+)$", text)
    if match:
        num = int(match.group(1))
        unit = match.group(2)
        for key, multiplier in time_units.items():
            if unit.startswith(key) or key.startswith(unit):
                return timedelta(seconds=num * multiplier)
        return None

    match = re.match(r"^(\d+)", text)
    if not match:
        return None

    num = int(match.group(1))
    rest = text[match.end():].strip()
    for unit, multiplier in time_units.items():
        if unit in rest:
            return timedelta(seconds=num * multiplier)

    return None


async def is_moderator(message: types.Message, required: int = 1) -> bool:
    if message.from_user.id == OWNER_ID:
        return True
    return await has_rank(message, required)


async def get_target(message: types.Message):
    if message.reply_to_message:
        return message.reply_to_message.from_user

    args = message.text.split()
    if len(args) < 2:
        return None

    target_username = args[1].lstrip("@")
    try:
        member = await message.chat.get_member(target_username)
        return member.user
    except Exception:
        return None


def get_duration_arg(message: types.Message):
    if not message.text:
        return None
    args = message.text.split()
    start_index = 1
    if len(args) > 1 and args[1].startswith("@"):
        start_index = 2
    time_parts = args[start_index:]
    if not time_parts:
        return None
    time_str = " ".join(time_parts)
    if not re.search(r"\d", time_str):
        return None
    return time_str


async def apply_filter_punishment(message: types.Message, reason: str):
    chat_id = message.chat.id
    mute_time = chat_filters[chat_id]["filter_mute_time"]
    try:
        await message.delete()
    except Exception:
        pass
    try:
        await message.bot.restrict_chat_member(
            chat_id=chat_id,
            user_id=message.from_user.id,
            permissions=ChatPermissions(can_send_messages=False),
            until_date=datetime.now() + timedelta(minutes=mute_time),
        )
        await message.answer(
            f"🚫 {message.from_user.mention_html()} замучен на {mute_time} мин. "
            f"Причина: {reason}"
        )
    except Exception:
        pass


async def apply_punishment(bot, chat_id: int, user_id: int, action: str, time_str: str, reason: str):
    try:
        if action == "mute":
            duration = parse_time(time_str) if time_str else None
            until = datetime.now() + duration if duration else datetime.now() + timedelta(days=3650)
            await bot.restrict_chat_member(
                chat_id=chat_id, user_id=user_id,
                permissions=ChatPermissions(can_send_messages=False),
                until_date=until,
            )
            await bot.send_message(chat_id, f"🔇 <a href='tg://user?id={user_id}'>Пользователь</a> замучен на {time_str or 'бессрочно'}. Причина: {reason}")
        elif action == "ban":
            await bot.ban_chat_member(chat_id, user_id)
            await bot.send_message(chat_id, f"🔨 <a href='tg://user?id={user_id}'>Пользователь</a> забанен на {time_str or 'навсегда'}. Причина: {reason}")
        elif action == "kick":
            await bot.ban_chat_member(chat_id, user_id)
            await bot.unban_chat_member(chat_id, user_id)
            await bot.send_message(chat_id, f"👢 <a href='tg://user?id={user_id}'>Пользователь</a> кикнут. Причина: {reason}")
        elif action == "warn":
            warns = add_warn(user_id, chat_id)
            await bot.send_message(chat_id, f"⚠️ <a href='tg://user?id={user_id}'>Пользователь</a> получил варн. Всего: {warns}. Причина: {reason}")
    except Exception as e:
        print(f"Punishment error: {e}")


# ==================== МУТ ====================
@router.message(lambda m: m.text and m.text.lower().startswith((
    "мут", ".мут", "!мут", "/мут", "mute", ".mute", "!mute", "/mute"
)))
async def cmd_mute(message: types.Message):
    if not await is_moderator(message, 1):
        await message.reply("❌ Недостаточно прав.")
        return

    target = await get_target(message)
    if not target:
        await message.reply("❌ Ответьте на сообщение или укажите @username.")
        return
    if target.id == OWNER_ID:
        await message.reply("❌ Нельзя замутить владельца.")
        return
    if target.id == message.from_user.id:
        await message.reply("❌ Нельзя замутить самого себя.")
        return

    duration, time_text, reason = parse_duration_reason(message)
    until = datetime.now() + duration if duration else datetime.now() + timedelta(days=3650)

    try:
        await message.bot.restrict_chat_member(
            chat_id=message.chat.id, user_id=target.id,
            permissions=ChatPermissions(can_send_messages=False),
            until_date=until,
        )
        try:
            log_action(message.chat.id, "mutes", message.from_user.id, target.id, time=time_text, reason=reason)
        except Exception:
            pass
        await message.reply(f"🔇 {target.mention_html()} замучен на {time_text}. Причина: {reason}.")
    except Exception as e:
        await message.reply(f"❌ Ошибка: {e}")


@router.message(lambda m: m.text and m.text.lower().startswith((
    "размут", ".размут", "!размут", "/размут",
    "унмут", ".унмут", "!унмут", "/унмут",
    "анмут", ".анмут", "!анмут", "/анмут",
    "unmute", ".unmute", "!unmute", "/unmute"
)))
async def cmd_unmute(message: types.Message):
    if not await is_moderator(message, 1):
        await message.reply("❌ Недостаточно прав.")
        return

    target = await get_target(message)
    if not target:
        await message.reply("❌ Ответьте на сообщение или укажите @username.")
        return

    try:
        await message.bot.restrict_chat_member(
            chat_id=message.chat.id, user_id=target.id,
            permissions=FULL_PERMISSIONS,
        )
        await message.reply(f"🔊 {target.mention_html()} размучен.")
    except Exception as e:
        await message.reply(f"❌ Ошибка: {e}")


# ==================== БАН ====================
@router.message(lambda m: m.text and m.text.lower().startswith((
    "бан", ".бан", "!бан", "/бан", "ban", ".ban", "!ban", "/ban"
)))
async def cmd_ban(message: types.Message):
    if not await is_moderator(message, 2):
        await message.reply("❌ Недостаточно прав.")
        return

    target = await get_target(message)
    if not target:
        await message.reply("❌ Ответьте на сообщение или укажите @username.")
        return
    if target.id == OWNER_ID:
        await message.reply("❌ Нельзя забанить владельца.")
        return

    time_arg = get_duration_arg(message)
    duration = parse_time(time_arg) if time_arg else None

    try:
        await message.bot.ban_chat_member(message.chat.id, target.id)
        if duration:
            await message.reply(f"🔨 {target.mention_html()} забанен на {time_arg}.")
        else:
            await message.reply(f"🔨 {target.mention_html()} забанен навсегда.")
    except Exception as e:
        await message.reply(f"❌ Ошибка: {e}")


@router.message(lambda m: m.text and m.text.lower().startswith((
    "разбан", ".разбан", "!разбан", "/разбан",
    "унбан", ".унбан", "!унбан", "/унбан",
    "unban", ".unban", "!unban", "/unban"
)))
async def cmd_unban(message: types.Message):
    if not await is_moderator(message, 2):
        await message.reply("❌ Недостаточно прав.")
        return

    target = await get_target(message)
    if not target:
        await message.reply("❌ Ответьте на сообщение или укажите @username.")
        return

    try:
        await message.bot.unban_chat_member(message.chat.id, target.id)
        await message.reply(f"✅ {target.mention_html()} разбанен.")
    except Exception as e:
        await message.reply(f"❌ Ошибка: {e}")


# ==================== КИК ====================
@router.message(lambda m: m.text and m.text.lower().startswith((
    "кик", ".кик", "!кик", "/кик", "kick", ".kick", "!kick", "/kick"
)))
async def cmd_kick(message: types.Message):
    if not await is_moderator(message, 1):
        await message.reply("❌ Недостаточно прав.")
        return

    target = await get_target(message)
    if not target:
        await message.reply("❌ Ответьте на сообщение или укажите @username.")
        return
    if target.id == OWNER_ID:
        await message.reply("❌ Нельзя кикнуть владельца.")
        return

    try:
        await message.bot.ban_chat_member(message.chat.id, target.id)
        await message.bot.unban_chat_member(message.chat.id, target.id)
        await message.reply(f"👢 {target.mention_html()} кикнут.")
    except Exception as e:
        await message.reply(f"❌ Ошибка: {e}")


# ==================== ВАРН ====================
@router.message(lambda m: m.text and m.text.lower().startswith((
    "варн", ".варн", "!варн", "/варн",
    "пред", ".пред", "!пред", "/пред",
    "warn", ".warn", "!warn", "/warn"
)))
async def cmd_warn(message: types.Message):
    if not await is_moderator(message, 1):
        await message.reply("❌ Недостаточно прав.")
        return

    target = await get_target(message)
    if not target:
        await message.reply("❌ Ответьте на сообщение или укажите @username.")
        return
    if target.id == OWNER_ID:
        await message.reply("❌ Нельзя выдать варн владельцу.")
        return

    warns = add_warn(target.id, message.chat.id)
    if warns is None:
        create_user(target.id, message.chat.id, target.username, target.first_name)
        warns = add_warn(target.id, message.chat.id)

    await message.reply(f"⚠️ {target.mention_html()} получил варн. Всего: {warns}")

    triggers = chat_triggers[message.chat.id]
    for trigger_key in ["warn_3", "warn_5", "warn_7", "warn_10"]:
        trigger = triggers.get(trigger_key)
        if not trigger or not trigger.get("enabled"):
            continue
        threshold = int(trigger_key.split("_")[1])
        if warns == threshold:
            await apply_punishment(
                message.bot, message.chat.id, target.id,
                trigger["action"], trigger["time"],
                f"достигнут лимит варнов ({threshold})",
            )
            reset_warns(target.id, message.chat.id)
            break


@router.message(lambda m: m.text and m.text.lower().startswith((
    "унварн", ".унварн", "!унварн", "/унварн",
    "анварн", ".анварн", "!анварн", "/анварн",
    "снятьварн", ".снятьварн", "!снятьварн", "/снятьварн",
    "unwarn", ".unwarn", "!unwarn", "/unwarn"
)))
async def cmd_unwarn(message: types.Message):
    if not await is_moderator(message, 2):
        await message.reply("❌ Недостаточно прав.")
        return

    target = await get_target(message)
    if not target:
        await message.reply("❌ Ответьте на сообщение или укажите @username.")
        return

    reset_warns(target.id, message.chat.id)
    await message.reply(f"✅ Варны {target.mention_html()} сняты.")


@router.message(lambda m: m.text and m.text.lower().startswith((
    "варны", ".варны", "!варны", "/варны",
    "преды", ".преды", "!преды", "/преды",
    "warns", ".warns", "!warns", "/warns"
)))
async def cmd_warns(message: types.Message):
    target = await get_target(message)
    if not target:
        target = message.from_user

    user = get_user(target.id, message.chat.id)
    if not user:
        await message.reply(f"У {target.mention_html()} нет предупреждений.")
        return

    await message.reply(f"⚠️ {target.mention_html()}: <b>{user['warns']}</b> предупреждений.")


@router.message(lambda m: m.text and m.text.lower().startswith((
    "повысить", ".повысить", "!повысить", "/повысить",
    "promote", ".promote", "!promote", "/promote"
)))
async def cmd_promote(message: types.Message):
    if not await is_moderator(message, 3):
        await message.reply("❌ Недостаточно прав.")
        return

    target = await get_target(message)
    if not target:
        await message.reply("❌ Ответьте на сообщение или укажите @username.")
        return

    current = await get_user_rank(target.id, message.chat.id)
    if current >= 5:
        await message.reply("Максимальный ранг.")
        return

    set_rank(target.id, message.chat.id, current + 1)
    await message.reply(f"⬆️ {target.mention_html()} повышен до <b>{RANK_NAMES[current + 1]}</b>")


@router.message(lambda m: m.text and m.text.lower().startswith((
    "понизить", ".понизить", "!понизить", "/понизить",
    "demote", ".demote", "!demote", "/demote"
)))
async def cmd_demote(message: types.Message):
    if not await is_moderator(message, 4):
        await message.reply("❌ Недостаточно прав.")
        return

    target = await get_target(message)
    if not target:
        await message.reply("❌ Ответьте на сообщение или укажите @username.")
        return

    current = await get_user_rank(target.id, message.chat.id)
    if current <= 0:
        return

    set_rank(target.id, message.chat.id, current - 1)
    await message.reply(f"⬇️ {target.mention_html()} понижен до <b>{RANK_NAMES[current - 1]}</b>")


@router.message(lambda m: m.text and m.text.lower().startswith((
    "мойранг", ".мойранг", "!мойранг", "/мойранг",
    "rank", ".rank", "!rank", "/rank",
    "ктоя", ".ктоя", "!ктоя", "/ктоя"
)))
async def cmd_rank(message: types.Message):
    target = message.reply_to_message.from_user if message.reply_to_message else message.from_user
    rank = await get_user_rank(target.id, message.chat.id)
    await message.reply(f"Ранг {target.mention_html()}: <b>{RANK_NAMES.get(rank, 'Участник')}</b>")


# ==================== ЭТАП 5: УПРАВЛЕНИЕ СООБЩЕНИЯМИ ====================
@router.message(lambda m: m.text and m.text.lower().startswith((
    "-смс", ".смс", "!смс", "/смс", "del", ".del", "!del", "/del"
)))
async def cmd_delete(message: types.Message):
    """Удалить сообщение, на которое ответили."""
    if not await is_moderator(message, 1):
        await message.reply("❌ Недостаточно прав.")
        return

    if not message.reply_to_message:
        await message.reply("❌ Ответьте на сообщение, которое нужно удалить.")
        return

    try:
        await message.reply_to_message.delete()
        await message.delete()
    except Exception as e:
        await message.reply(f"❌ Ошибка: {e}")


@router.message(lambda m: m.text and m.text.lower().startswith((
    ".пин", "!пин", "/пин", "pin", ".pin", "!pin", "/pin"
)))
async def cmd_pin(message: types.Message):
    """Закрепить сообщение."""
    if not await is_moderator(message, 2):
        await message.reply("❌ Недостаточно прав.")
        return

    if not message.reply_to_message:
        await message.reply("❌ Ответьте на сообщение для закрепления.")
        return

    try:
        await message.bot.pin_chat_message(
            chat_id=message.chat.id,
            message_id=message.reply_to_message.message_id,
            disable_notification=False,
        )
        await message.reply("📌 Закреплено.")
    except Exception as e:
        await message.reply(f"❌ Ошибка: {e}")


@router.message(lambda m: m.text and m.text.lower().startswith((
    ".анпин", "!анпин", "/анпин", "unpin", ".unpin", "!unpin", "/unpin"
)))
async def cmd_unpin(message: types.Message):
    """Открепить сообщение."""
    if not await is_moderator(message, 2):
        await message.reply("❌ Недостаточно прав.")
        return

    try:
        await message.bot.unpin_chat_message(chat_id=message.chat.id)
        await message.reply("📌 Откреплено.")
    except Exception as e:
        await message.reply(f"❌ Ошибка: {e}")


@router.message(lambda m: m.text and m.text.lower().startswith((
    "очистить", ".очистить", "!очистить", "/очистить", "clear", ".clear"
)))
async def cmd_clear(message: types.Message):
    """Удалить последние N сообщений."""
    if not await is_moderator(message, 2):
        await message.reply("❌ Недостаточно прав.")
        return

    args = message.text.split()
    if len(args) < 2 or not args[1].isdigit():
        await message.reply("❌ Формат: <code>очистить 10</code>")
        return

    count = min(int(args[1]), 100)
    deleted = 0

    try:
        # Удаляем сообщения от последнего к раннему
        for offset in range(1, count + 1):
            try:
                await message.bot.delete_message(
                    chat_id=message.chat.id,
                    message_id=message.message_id - offset,
                )
                deleted += 1
            except Exception:
                pass
        await message.answer(f"🧹 Удалено сообщений: {deleted}.")
    except Exception as e:
        await message.reply(f"❌ Ошибка: {e}")


# ==================== ЭТАП 5: ИНФОРМАЦИЯ ====================
@router.message(lambda m: m.text and m.text.lower().startswith((
    "кто админ", ".кто админ", "!кто админ", "ктоадмин",
    "кто админы", ".кто админы", "ктоадмины",
    "admins", ".admins", "!admins"
)))
async def cmd_admins(message: types.Message):
    """Показать список администраторов."""
    try:
        admins = await message.chat.get_administrators()
        text = "<b>👑 Администраторы чата:</b>\n\n"
        for admin in admins:
            user = admin.user
            if user.is_bot:
                continue
            status = "Создатель" if admin.status == "creator" else "Админ"
            text += f"• {user.mention_html()} — {status}\n"
        await message.reply(text)
    except Exception as e:
        await message.reply(f"❌ Ошибка: {e}")


@router.message(lambda m: m.text and m.text.lower().startswith((
    "кто модер", ".кто модер", "!кто модер", "ктомодер",
    "модеры", ".модеры", "модераторы", ".модераторы",
    "moders", ".moders", "!moders"
)))
async def cmd_moders(message: types.Message):
    """Показать список модераторов (по рангам в БД)."""
    try:
        # Показываем админов как модеров
        admins = await message.chat.get_administrators()
        text = "<b>🛡️ Модерация чата:</b>\n\n"
        for admin in admins:
            user = admin.user
            if user.is_bot:
                continue
            rank = await get_user_rank(user.id, message.chat.id)
            rank_name = RANK_NAMES.get(rank, "Участник")
            text += f"• {user.mention_html()} — {rank_name}\n"
        await message.reply(text)
    except Exception as e:
        await message.reply(f"❌ Ошибка: {e}")


@router.message(lambda m: m.text and m.text.lower().startswith((
    "инфо", ".инфо", "!инфо", "/инфо", "info", ".info", "!info"
)))
async def cmd_info(message: types.Message):
    """Информация о пользователе."""
    target = await get_target(message)
    if not target:
        target = message.from_user

    user = get_user(target.id, message.chat.id)
    if not user:
        create_user(target.id, message.chat.id, target.username, target.first_name)
        user = get_user(target.id, message.chat.id)

    rank = await get_user_rank(target.id, message.chat.id)
    rank_name = RANK_NAMES.get(rank, "Участник")

    text = (
        f"<b>👤 Информация о пользователе</b>\n\n"
        f"• Имя: {target.full_name}\n"
        f"• Username: @{target.username or 'нет'}\n"
        f"• ID: <code>{target.id}</code>\n"
        f"• Ранг: <b>{rank_name}</b>\n"
        f"• Варнов: <b>{user['warns']}</b>\n"
        f"• Баланс: <b>{user.get('balance', 0)}</b> ирисок\n"
    )
    await message.reply(text)


@router.message(lambda m: m.text and m.text.lower().startswith((
    "стата", ".стата", "!стата", "/стата", "stats", ".stats", "!stats"
)))
async def cmd_stats(message: types.Message):
    """Статистика пользователя или чата."""
    target = await get_target(message)

    if target:
        # Статистика пользователя
        user = get_user(target.id, message.chat.id)
        if not user:
            await message.reply(f"У {target.mention_html()} нет статистики.")
            return

        rank = await get_user_rank(target.id, message.chat.id)
        rank_name = RANK_NAMES.get(rank, "Участник")

        text = (
            f"<b>📊 Статистика {target.full_name}</b>\n\n"
            f"• Ранг: <b>{rank_name}</b>\n"
            f"• Варнов: <b>{user['warns']}</b>\n"
            f"• Баланс: <b>{user.get('balance', 0)}</b> ирисок\n"
        )
        await message.reply(text)
    else:
        # Статистика чата
        stats = chat_stats[message.chat.id]
        text = (
            f"<b>📊 Статистика чата</b>\n\n"
            f"• Сообщений: <b>{stats['messages']}</b>\n"
            f"• Участников: <b>{len(stats['users'])}</b>\n"
        )
        await message.reply(text)


@router.message(lambda m: m.text and m.text.lower().startswith((
    "топ", ".топ", "!топ", "/топ", "top", ".top", "!top"
)))
async def cmd_top(message: types.Message):
    """Топ активных участников."""
    stats = chat_stats[message.chat.id]
    if not stats["users"]:
        await message.reply("Пока нет статистики.")
        return

    sorted_users = sorted(stats["users"].items(), key=lambda x: x[1], reverse=True)[:10]
    text = "<b>🏆 Топ активных:</b>\n\n"
    for i, (user_id, count) in enumerate(sorted_users, 1):
        try:
            member = await message.chat.get_member(user_id)
            text += f"{i}. {member.user.mention_html()} — {count} сообщений\n"
        except Exception:
            text += f"{i}. ID {user_id} — {count} сообщений\n"
    await message.reply(text)


# ==================== УПРАВЛЕНИЕ ФИЛЬТРАМИ ====================
# (сокращённо — все команды этапов 2-4 включены)
@router.message(lambda m: m.text and m.text.lower().startswith(("+антиссылка", ".антиссылка")))
async def cmd_antilink_on(message: types.Message):
    if not await is_moderator(message, 2):
        return
    chat_filters[message.chat.id]["antilink"] = True
    await message.reply("✅ Антиссылки включены.")


@router.message(lambda m: m.text and m.text.lower().startswith(("-антиссылка", ".антиссылка выкл")))
async def cmd_antilink_off(message: types.Message):
    if not await is_moderator(message, 2):
        return
    chat_filters[message.chat.id]["antilink"] = False
    await message.reply("❌ Антиссылки выключены.")


@router.message(lambda m: m.text and m.text.lower().startswith(("+антимат", ".антимат")))
async def cmd_antimat_on(message: types.Message):
    if not await is_moderator(message, 2):
        return
    chat_filters[message.chat.id]["antimat"] = True
    await message.reply("✅ Антимат включён.")


@router.message(lambda m: m.text and m.text.lower().startswith(("-антимат", ".антимат выкл")))
async def cmd_antimat_off(message: types.Message):
    if not await is_moderator(message, 2):
        return
    chat_filters[message.chat.id]["antimat"] = False
    await message.reply("❌ Антимат выключен.")


@router.message(lambda m: m.text and m.text.lower().startswith(("+антифлуд", ".антифлуд")))
async def cmd_antiflood_on(message: types.Message):
    if not await is_moderator(message, 2):
        return
    chat_filters[message.chat.id]["antiflood"] = True
    await message.reply("✅ Антифлуд включён.")


@router.message(lambda m: m.text and m.text.lower().startswith(("-антифлуд", ".антифлуд выкл")))
async def cmd_antiflood_off(message: types.Message):
    if not await is_moderator(message, 2):
        return
    chat_filters[message.chat.id]["antiflood"] = False
    await message.reply("❌ Антифлуд выключен.")


@router.message(lambda m: m.text and m.text.lower().startswith(("+антикапс", ".антикапс")))
async def cmd_anticaps_on(message: types.Message):
    if not await is_moderator(message, 2):
        return
    chat_filters[message.chat.id]["anticaps"] = True
    await message.reply("✅ Антикапс включён.")


@router.message(lambda m: m.text and m.text.lower().startswith(("-антикапс", ".антикапс выкл")))
async def cmd_anticaps_off(message: types.Message):
    if not await is_moderator(message, 2):
        return
    chat_filters[message.chat.id]["anticaps"] = False
    await message.reply("❌ Антикапс выключен.")


# ==================== ТРИГГЕРЫ ====================
@router.message(lambda m: m.text and m.text.lower().startswith((
    "триггер варн", ".триггер варн", "!триггер варн"
)))
async def cmd_set_warn_trigger(message: types.Message):
    if not await is_moderator(message, 3):
        await message.reply("❌ Недостаточно прав.")
        return

    args = message.text.split()
    if len(args) < 5:
        await message.reply("Формат: <code>триггер варн 3 мут 1ч</code>")
        return

    try:
        threshold = int(args[2])
    except ValueError:
        await message.reply("❌ Число варнов должно быть числом.")
        return

    action_map = {"мут": "mute", "бан": "ban", "кик": "kick", "варн": "warn"}
    action = action_map.get(args[3].lower())
    if not action:
        await message.reply("❌ Действие: мут, бан, кик, варн")
        return

    time_str = args[4] if len(args) > 4 else "0"
    key = f"warn_{threshold}"
    chat_triggers[message.chat.id][key] = {
        "action": action, "time": time_str, "enabled": True,
    }
    await message.reply(f"✅ Триггер: <b>{threshold}</b> варнов → <b>{args[3]}</b> на <b>{time_str}</b>")


@router.message(lambda m: m.text and m.text.lower().startswith((
    "триггеры", ".триггеры", "!триггеры"
)))
async def cmd_list_triggers(message: types.Message):
    triggers = chat_triggers[message.chat.id]
    text = "<b>📋 Триггеры чата:</b>\n\n"

    for key in ["warn_3", "warn_5", "warn_7", "warn_10"]:
        t = triggers.get(key)
        if t and t.get("enabled"):
            threshold = key.split("_")[1]
            text += f"• {threshold} варнов → {t['action']} на {t['time']}\n"

    if triggers["link_enabled"]:
        text += f"• Ссылка → {triggers['link_action']} на {triggers['link_time']}\n"

    if triggers["mat_enabled"]:
        text += f"• Мат → {triggers['mat_action']} на {triggers['mat_time']}\n"

    await message.reply(text)


# ==================== АВТОФИЛЬТРЫ + СТАТИСТИКА ====================
# PATCH: earlier catch-all decorator removed so commands and final filters can run.
async def auto_filters(message: types.Message):
    if message.from_user.id == OWNER_ID:
        return

    chat_id = message.chat.id

    # Статистика
    chat_stats[chat_id]["messages"] += 1
    chat_stats[chat_id]["users"][message.from_user.id] += 1

    if await has_rank(message, 1):
        return

    filters = chat_filters[chat_id]
    triggers = chat_triggers[chat_id]

    if filters["antibot"] and message.from_user.is_bot:
        try:
            await message.bot.ban_chat_member(chat_id, message.from_user.id)
            await message.answer(f"🤖 Бот {message.from_user.mention_html()} забанен.")
        except Exception:
            pass
        return

    if filters["antisticker"] and message.sticker:
        await apply_filter_punishment(message, "стикер")
        return
    if filters["antigif"] and message.animation:
        await apply_filter_punishment(message, "GIF")
        return
    if filters["antiphoto"] and message.photo:
        await apply_filter_punishment(message, "фото")
        return
    if filters["antivideo"] and message.video:
        await apply_filter_punishment(message, "видео")
        return
    if filters["antivoice"] and message.voice:
        await apply_filter_punishment(message, "голосовое")
        return
    if filters["antidocument"] and message.document:
        await apply_filter_punishment(message, "документ")
        return

    if not message.text:
        return

    text = message.text
    lower_text = text.lower()

    if triggers["link_enabled"] and URL_PATTERN.search(text):
        await apply_punishment(
            message.bot, chat_id, message.from_user.id,
            triggers["link_action"], triggers["link_time"], "ссылка в чате",
        )
        return

    if triggers["mat_enabled"]:
        for word in BAD_WORDS:
            if word in lower_text:
                await apply_punishment(
                    message.bot, chat_id, message.from_user.id,
                    triggers["mat_action"], triggers["mat_time"], "нецензурная лексика",
                )
                return

    if filters["antiall"] and ALL_PATTERN.search(text):
        await apply_filter_punishment(message, "@all")
        return

    if filters["antilink"] and URL_PATTERN.search(text):
        await apply_filter_punishment(message, "ссылка")
        return

    if filters["antiinvite"] and ("t.me/joinchat" in lower_text or "telegram.me/joinchat" in lower_text):
        await apply_filter_punishment(message, "инвайт")
        return

    if filters["antimat"]:
        for word in BAD_WORDS:
            if word in lower_text:
                await apply_filter_punishment(message, "мат")
                return

    if filters["antireklama"]:
        for word in REKLAMA_WORDS:
            if word in lower_text:
                await apply_filter_punishment(message, "реклама")
                return

    if filters["antikripta"]:
        for word in KRIPTA_WORDS:
            if word in lower_text:
                await apply_filter_punishment(message, "крипта")
                return

    if filters["antifishing"]:
        for pattern in FISHING_PATTERNS:
            if re.search(pattern, lower_text):
                await apply_filter_punishment(message, "фишинг")
                return

    if filters["anticaps"] and len(text) > 5:
        letters = [c for c in text if c.isalpha()]
        if letters:
            caps = sum(1 for c in letters if c.isupper())
            percent = (caps / len(letters)) * 100
            if percent >= filters["anticaps_percent"]:
                await apply_filter_punishment(message, "капс")
                return

    if filters["antiflood"]:
        now = datetime.now()
        window = filters["antiflood_window"]
        limit = filters["antiflood_limit"]

        flood_tracker[chat_id][message.from_user.id] = [
            t for t in flood_tracker[chat_id][message.from_user.id]
            if (now - t).total_seconds() < window
        ]
        flood_tracker[chat_id][message.from_user.id].append(now)

        if len(flood_tracker[chat_id][message.from_user.id]) > limit:
            try:
                await message.bot.restrict_chat_member(
                    chat_id=chat_id, user_id=message.from_user.id,
                    permissions=ChatPermissions(can_send_messages=False),
                    until_date=now + timedelta(minutes=5),
                )
                await message.answer(f"🌊 {message.from_user.mention_html()} замучен на 5 минут за флуд.")
                flood_tracker[chat_id][message.from_user.id] = []
            except Exception:
                pass
                """
Модуль модерации. Этап 6: управление рангами и правами.
"""

import re
from datetime import datetime, timedelta
from collections import defaultdict
from aiogram import Router, types
from aiogram.types import ChatPermissions
from database.crud import (
    get_user, create_user, add_warn, reset_warns, get_chat, set_rank,
)
from middlewares.check_rank import has_rank, get_user_rank, OWNER_ID

# PATCH: router reset removed; all handlers share the first router.

RANK_NAMES = {
    0: "Участник",
    1: "Младший модератор",
    2: "Старший модератор",
    3: "Младший администратор",
    4: "Старший администратор",
    5: "Создатель",
}

FULL_PERMISSIONS = ChatPermissions(
    can_send_messages=True, can_send_media_messages=True,
    can_send_other_messages=True, can_add_web_page_previews=True,
    can_send_polls=True, can_change_info=False,
    can_invite_users=True, can_pin_messages=False,
)

# ==================== ХРАНИЛИЩА ====================
chat_filters = defaultdict(lambda: {
    "antilink": False, "antimat": False,
    "antiflood": False, "antiflood_limit": 5, "antiflood_window": 10,
    "anticaps": False, "anticaps_percent": 70,
    "antisticker": False, "antigif": False, "antiphoto": False,
    "antivideo": False, "antivoice": False, "antidocument": False,
    "antibot": False, "antireklama": False, "antikripta": False,
    "antifishing": False, "antiinvite": False, "antiall": False,
    "filter_mute_time": 10,
})

chat_triggers = defaultdict(lambda: {
    "warn_3": {"action": "mute", "time": "1ч", "enabled": True},
    "warn_5": {"action": "mute", "time": "24ч", "enabled": True},
    "warn_7": {"action": "ban", "time": "7д", "enabled": True},
    "warn_10": {"action": "ban", "time": "0", "enabled": True},
    "link_action": "ban", "link_time": "30д", "link_enabled": False,
    "mat_action": "mute", "mat_time": "30м", "mat_enabled": False,
    "warn_limit": 3,
})

flood_tracker = defaultdict(lambda: defaultdict(list))

chat_stats = defaultdict(lambda: {
    "messages": 0, "users": defaultdict(int), "created": datetime.now(),
})

# ==================== СПИСКИ ====================
BAD_WORDS = [
    "хуй", "пизд", "бляд", "ебал", "ебат", "сука", "блядь", "мраз",
    "fuck", "shit", "bitch", "asshole", "dick", "pussy",
]

REKLAMA_WORDS = [
    "купить", "продам", "продаю", "скидка", "акция", "распродажа",
    "заработок", "заработать", "инвестиции", "вложить", "крипта",
    "btc", "bitcoin", "eth", "ethereum", "usdt", "toncoin", "ton",
    "казино", "ставки", "букмекер", "1xbet", "betting",
    "подпишись", "подписка", "канал", "чат", "переходи",
    "промокод", "реферал", "рефка", "приглашаю",
]

KRIPTA_WORDS = [
    "btc", "bitcoin", "eth", "ethereum", "usdt", "toncoin", "ton",
    "crypto", "крипта", "кошелёк", "wallet", "binance", "bybit",
    "p2p", "обменник", "майнинг", "mining", "нфт", "nft",
]

FISHING_PATTERNS = [
    r"t\.me/joinchat", r"telegram\.me/joinchat",
    r"bit\.ly", r"tinyurl", r"goo\.gl", r"shorturl",
    r"free.*ton", r"bonus.*ton", r"gift.*ton",
    r"airdrop", r"giveaway", r"claim.*reward",
    r"verify.*wallet", r"connect.*wallet", r"seed.*phrase",
]

URL_PATTERN = re.compile(r"(https?://|www\.|t\.me/|telegram\.me/)", re.IGNORECASE)
ALL_PATTERN = re.compile(r"@(all|everyone|here)", re.IGNORECASE)


# ==================== ПАРСЕР ВРЕМЕНИ ====================
def parse_time(text: str):
    if not text:
        return None
    text = text.lower().strip()

    time_units = {
        "сек": 1, "секунда": 1, "секунды": 1, "секунд": 1, "с": 1,
        "мин": 60, "минута": 60, "минуты": 60, "минут": 60, "м": 60,
        "час": 3600, "часа": 3600, "часов": 3600, "ч": 3600,
        "день": 86400, "дня": 86400, "дней": 86400, "д": 86400, "сутки": 86400,
        "нед": 604800, "неделя": 604800, "недели": 604800, "недель": 604800, "н": 604800,
        "мес": 2592000, "месяц": 2592000, "месяца": 2592000, "месяцев": 2592000,
        "год": 31536000, "года": 31536000, "лет": 31536000, "г": 31536000,
    }

    match = re.match(r"^(\d+)\s*([а-яa-z]+)$", text)
    if match:
        num = int(match.group(1))
        unit = match.group(2)
        for key, multiplier in time_units.items():
            if unit.startswith(key) or key.startswith(unit):
                return timedelta(seconds=num * multiplier)
        return None

    match = re.match(r"^(\d+)", text)
    if not match:
        return None

    num = int(match.group(1))
    rest = text[match.end():].strip()
    for unit, multiplier in time_units.items():
        if unit in rest:
            return timedelta(seconds=num * multiplier)

    return None


async def is_moderator(message: types.Message, required: int = 1) -> bool:
    if message.from_user.id == OWNER_ID:
        return True
    return await has_rank(message, required)


async def get_target(message: types.Message):
    if message.reply_to_message:
        return message.reply_to_message.from_user

    args = message.text.split()
    if len(args) < 2:
        return None

    target_username = args[1].lstrip("@")
    try:
        member = await message.chat.get_member(target_username)
        return member.user
    except Exception:
        return None


def get_duration_arg(message: types.Message):
    if not message.text:
        return None
    args = message.text.split()
    start_index = 1
    if len(args) > 1 and args[1].startswith("@"):
        start_index = 2
    time_parts = args[start_index:]
    if not time_parts:
        return None
    time_str = " ".join(time_parts)
    if not re.search(r"\d", time_str):
        return None
    return time_str


async def apply_filter_punishment(message: types.Message, reason: str):
    chat_id = message.chat.id
    mute_time = chat_filters[chat_id]["filter_mute_time"]
    try:
        await message.delete()
    except Exception:
        pass
    try:
        await message.bot.restrict_chat_member(
            chat_id=chat_id, user_id=message.from_user.id,
            permissions=ChatPermissions(can_send_messages=False),
            until_date=datetime.now() + timedelta(minutes=mute_time),
        )
        await message.answer(f"🚫 {message.from_user.mention_html()} замучен на {mute_time} мин. Причина: {reason}")
    except Exception:
        pass


async def apply_punishment(bot, chat_id: int, user_id: int, action: str, time_str: str, reason: str):
    try:
        if action == "mute":
            duration = parse_time(time_str) if time_str else None
            until = datetime.now() + duration if duration else datetime.now() + timedelta(days=3650)
            await bot.restrict_chat_member(
                chat_id=chat_id, user_id=user_id,
                permissions=ChatPermissions(can_send_messages=False),
                until_date=until,
            )
            await bot.send_message(chat_id, f"🔇 <a href='tg://user?id={user_id}'>Пользователь</a> замучен на {time_str or 'бессрочно'}. Причина: {reason}")
        elif action == "ban":
            await bot.ban_chat_member(chat_id, user_id)
            await bot.send_message(chat_id, f"🔨 <a href='tg://user?id={user_id}'>Пользователь</a> забанен. Причина: {reason}")
        elif action == "kick":
            await bot.ban_chat_member(chat_id, user_id)
            await bot.unban_chat_member(chat_id, user_id)
            await bot.send_message(chat_id, f"👢 <a href='tg://user?id={user_id}'>Пользователь</a> кикнут. Причина: {reason}")
        elif action == "warn":
            warns = add_warn(user_id, chat_id)
            await bot.send_message(chat_id, f"⚠️ <a href='tg://user?id={user_id}'>Пользователь</a> получил варн. Всего: {warns}. Причина: {reason}")
    except Exception as e:
        print(f"Punishment error: {e}")


# ==================== МУТ ====================
@router.message(lambda m: m.text and m.text.lower().startswith((
    "мут", ".мут", "!мут", "/мут", "mute", ".mute", "!mute", "/mute"
)))
async def cmd_mute(message: types.Message):
    if not await is_moderator(message, 1):
        await message.reply("❌ Недостаточно прав.")
        return

    target = await get_target(message)
    if not target:
        await message.reply("❌ Ответьте на сообщение или укажите @username.")
        return
    if target.id == OWNER_ID:
        await message.reply("❌ Нельзя замутить владельца.")
        return
    if target.id == message.from_user.id:
        await message.reply("❌ Нельзя замутить самого себя.")
        return

    duration, time_text, reason = parse_duration_reason(message)
    until = datetime.now() + duration if duration else datetime.now() + timedelta(days=3650)

    try:
        await message.bot.restrict_chat_member(
            chat_id=message.chat.id, user_id=target.id,
            permissions=ChatPermissions(can_send_messages=False),
            until_date=until,
        )
        try:
            log_action(message.chat.id, "mutes", message.from_user.id, target.id, time=time_text, reason=reason)
        except Exception:
            pass
        await message.reply(f"🔇 {target.mention_html()} замучен на {time_text}. Причина: {reason}.")
    except Exception as e:
        await message.reply(f"❌ Ошибка: {e}")


@router.message(lambda m: m.text and m.text.lower().startswith((
    "размут", ".размут", "!размут", "/размут",
    "унмут", ".унмут", "!унмут", "/унмут",
    "анмут", ".анмут", "!анмут", "/анмут",
    "unmute", ".unmute", "!unmute", "/unmute"
)))
async def cmd_unmute(message: types.Message):
    if not await is_moderator(message, 1):
        await message.reply("❌ Недостаточно прав.")
        return

    target = await get_target(message)
    if not target:
        await message.reply("❌ Ответьте на сообщение или укажите @username.")
        return

    try:
        await message.bot.restrict_chat_member(
            chat_id=message.chat.id, user_id=target.id,
            permissions=FULL_PERMISSIONS,
        )
        await message.reply(f"🔊 {target.mention_html()} размучен.")
    except Exception as e:
        await message.reply(f"❌ Ошибка: {e}")


# ==================== БАН ====================
@router.message(lambda m: m.text and m.text.lower().startswith((
    "бан", ".бан", "!бан", "/бан", "ban", ".ban", "!ban", "/ban"
)))
async def cmd_ban(message: types.Message):
    if not await is_moderator(message, 2):
        await message.reply("❌ Недостаточно прав.")
        return

    target = await get_target(message)
    if not target:
        await message.reply("❌ Ответьте на сообщение или укажите @username.")
        return
    if target.id == OWNER_ID:
        await message.reply("❌ Нельзя забанить владельца.")
        return

    time_arg = get_duration_arg(message)
    duration = parse_time(time_arg) if time_arg else None

    try:
        await message.bot.ban_chat_member(message.chat.id, target.id)
        if duration:
            await message.reply(f"🔨 {target.mention_html()} забанен на {time_arg}.")
        else:
            await message.reply(f"🔨 {target.mention_html()} забанен навсегда.")
    except Exception as e:
        await message.reply(f"❌ Ошибка: {e}")


@router.message(lambda m: m.text and m.text.lower().startswith((
    "разбан", ".разбан", "!разбан", "/разбан",
    "унбан", ".унбан", "!унбан", "/унбан",
    "unban", ".unban", "!unban", "/unban"
)))
async def cmd_unban(message: types.Message):
    if not await is_moderator(message, 2):
        await message.reply("❌ Недостаточно прав.")
        return

    target = await get_target(message)
    if not target:
        await message.reply("❌ Ответьте на сообщение или укажите @username.")
        return

    try:
        await message.bot.unban_chat_member(message.chat.id, target.id)
        await message.reply(f"✅ {target.mention_html()} разбанен.")
    except Exception as e:
        await message.reply(f"❌ Ошибка: {e}")


# ==================== КИК ====================
@router.message(lambda m: m.text and m.text.lower().startswith((
    "кик", ".кик", "!кик", "/кик", "kick", ".kick", "!kick", "/kick"
)))
async def cmd_kick(message: types.Message):
    if not await is_moderator(message, 1):
        await message.reply("❌ Недостаточно прав.")
        return

    target = await get_target(message)
    if not target:
        await message.reply("❌ Ответьте на сообщение или укажите @username.")
        return
    if target.id == OWNER_ID:
        await message.reply("❌ Нельзя кикнуть владельца.")
        return

    try:
        await message.bot.ban_chat_member(message.chat.id, target.id)
        await message.bot.unban_chat_member(message.chat.id, target.id)
        await message.reply(f"👢 {target.mention_html()} кикнут.")
    except Exception as e:
        await message.reply(f"❌ Ошибка: {e}")


# ==================== ВАРН ====================
@router.message(lambda m: m.text and m.text.lower().startswith((
    "варн", ".варн", "!варн", "/варн",
    "пред", ".пред", "!пред", "/пред",
    "warn", ".warn", "!warn", "/warn"
)))
async def cmd_warn(message: types.Message):
    if not await is_moderator(message, 1):
        await message.reply("❌ Недостаточно прав.")
        return

    target = await get_target(message)
    if not target:
        await message.reply("❌ Ответьте на сообщение или укажите @username.")
        return
    if target.id == OWNER_ID:
        await message.reply("❌ Нельзя выдать варн владельцу.")
        return

    warns = add_warn(target.id, message.chat.id)
    if warns is None:
        create_user(target.id, message.chat.id, target.username, target.first_name)
        warns = add_warn(target.id, message.chat.id)

    await message.reply(f"⚠️ {target.mention_html()} получил варн. Всего: {warns}")

    triggers = chat_triggers[message.chat.id]
    for trigger_key in ["warn_3", "warn_5", "warn_7", "warn_10"]:
        trigger = triggers.get(trigger_key)
        if not trigger or not trigger.get("enabled"):
            continue
        threshold = int(trigger_key.split("_")[1])
        if warns == threshold:
            await apply_punishment(
                message.bot, message.chat.id, target.id,
                trigger["action"], trigger["time"],
                f"достигнут лимит варнов ({threshold})",
            )
            reset_warns(target.id, message.chat.id)
            break


@router.message(lambda m: m.text and m.text.lower().startswith((
    "унварн", ".унварн", "!унварн", "/унварн",
    "анварн", ".анварн", "!анварн", "/анварн",
    "снятьварн", ".снятьварн", "!снятьварн", "/снятьварн",
    "unwarn", ".unwarn", "!unwarn", "/unwarn"
)))
async def cmd_unwarn(message: types.Message):
    if not await is_moderator(message, 2):
        await message.reply("❌ Недостаточно прав.")
        return

    target = await get_target(message)
    if not target:
        await message.reply("❌ Ответьте на сообщение или укажите @username.")
        return

    reset_warns(target.id, message.chat.id)
    await message.reply(f"✅ Варны {target.mention_html()} сняты.")


@router.message(lambda m: m.text and m.text.lower().startswith((
    "варны", ".варны", "!варны", "/варны",
    "преды", ".преды", "!преды", "/преды",
    "warns", ".warns", "!warns", "/warns"
)))
async def cmd_warns(message: types.Message):
    target = await get_target(message)
    if not target:
        target = message.from_user

    user = get_user(target.id, message.chat.id)
    if not user:
        await message.reply(f"У {target.mention_html()} нет предупреждений.")
        return

    await message.reply(f"⚠️ {target.mention_html()}: <b>{user['warns']}</b> предупреждений.")


@router.message(lambda m: m.text and m.text.lower().startswith((
    "повысить", ".повысить", "!повысить", "/повысить",
    "promote", ".promote", "!promote", "/promote"
)))
async def cmd_promote(message: types.Message):
    if not await is_moderator(message, 3):
        await message.reply("❌ Недостаточно прав.")
        return

    target = await get_target(message)
    if not target:
        await message.reply("❌ Ответьте на сообщение или укажите @username.")
        return

    current = await get_user_rank(target.id, message.chat.id)
    if current >= 5:
        await message.reply("Максимальный ранг.")
        return

    set_rank(target.id, message.chat.id, current + 1)
    await message.reply(f"⬆️ {target.mention_html()} повышен до <b>{RANK_NAMES[current + 1]}</b>")


@router.message(lambda m: m.text and m.text.lower().startswith((
    "понизить", ".понизить", "!понизить", "/понизить",
    "demote", ".demote", "!demote", "/demote"
)))
async def cmd_demote(message: types.Message):
    if not await is_moderator(message, 4):
        await message.reply("❌ Недостаточно прав.")
        return

    target = await get_target(message)
    if not target:
        await message.reply("❌ Ответьте на сообщение или укажите @username.")
        return

    current = await get_user_rank(target.id, message.chat.id)
    if current <= 0:
        return

    set_rank(target.id, message.chat.id, current - 1)
    await message.reply(f"⬇️ {target.mention_html()} понижен до <b>{RANK_NAMES[current - 1]}</b>")


@router.message(lambda m: m.text and m.text.lower().startswith((
    "мойранг", ".мойранг", "!мойранг", "/мойранг",
    "rank", ".rank", "!rank", "/rank",
    "ктоя", ".ктоя", "!ктоя", "/ктоя"
)))
async def cmd_rank(message: types.Message):
    target = message.reply_to_message.from_user if message.reply_to_message else message.from_user
    rank = await get_user_rank(target.id, message.chat.id)
    await message.reply(f"Ранг {target.mention_html()}: <b>{RANK_NAMES.get(rank, 'Участник')}</b>")


# ==================== ЭТАП 5: УПРАВЛЕНИЕ СООБЩЕНИЯМИ ====================
@router.message(lambda m: m.text and m.text.lower().startswith((
    "-смс", ".смс", "!смс", "/смс", "del", ".del", "!del", "/del"
)))
async def cmd_delete(message: types.Message):
    if not await is_moderator(message, 1):
        await message.reply("❌ Недостаточно прав.")
        return
    if not message.reply_to_message:
        await message.reply("❌ Ответьте на сообщение, которое нужно удалить.")
        return
    try:
        await message.reply_to_message.delete()
        await message.delete()
    except Exception as e:
        await message.reply(f"❌ Ошибка: {e}")


@router.message(lambda m: m.text and m.text.lower().startswith((
    ".пин", "!пин", "/пин", "pin", ".pin", "!pin", "/pin"
)))
async def cmd_pin(message: types.Message):
    if not await is_moderator(message, 2):
        await message.reply("❌ Недостаточно прав.")
        return
    if not message.reply_to_message:
        await message.reply("❌ Ответьте на сообщение для закрепления.")
        return
    try:
        await message.bot.pin_chat_message(
            chat_id=message.chat.id,
            message_id=message.reply_to_message.message_id,
        )
        await message.reply("📌 Закреплено.")
    except Exception as e:
        await message.reply(f"❌ Ошибка: {e}")


@router.message(lambda m: m.text and m.text.lower().startswith((
    ".анпин", "!анпин", "/анпин", "unpin", ".unpin", "!unpin", "/unpin"
)))
async def cmd_unpin(message: types.Message):
    if not await is_moderator(message, 2):
        await message.reply("❌ Недостаточно прав.")
        return
    try:
        await message.bot.unpin_chat_message(chat_id=message.chat.id)
        await message.reply("📌 Откреплено.")
    except Exception as e:
        await message.reply(f"❌ Ошибка: {e}")


@router.message(lambda m: m.text and m.text.lower().startswith((
    "очистить", ".очистить", "!очистить", "/очистить", "clear", ".clear"
)))
async def cmd_clear(message: types.Message):
    if not await is_moderator(message, 2):
        await message.reply("❌ Недостаточно прав.")
        return

    args = message.text.split()
    if len(args) < 2 or not args[1].isdigit():
        await message.reply("❌ Формат: <code>очистить 10</code>")
        return

    count = min(int(args[1]), 100)
    deleted = 0
    try:
        for offset in range(1, count + 1):
            try:
                await message.bot.delete_message(
                    chat_id=message.chat.id,
                    message_id=message.message_id - offset,
                )
                deleted += 1
            except Exception:
                pass
        await message.answer(f"🧹 Удалено сообщений: {deleted}.")
    except Exception as e:
        await message.reply(f"❌ Ошибка: {e}")


# ==================== ЭТАП 5: ИНФОРМАЦИЯ ====================
@router.message(lambda m: m.text and m.text.lower().startswith((
    "кто админ", ".кто админ", "!кто админ", "ктоадмин", "ктоадмины",
    "admins", ".admins", "!admins"
)))
async def cmd_admins(message: types.Message):
    try:
        admins = await message.chat.get_administrators()
        text = "<b>👑 Администраторы чата:</b>\n\n"
        for admin in admins:
            user = admin.user
            if user.is_bot:
                continue
            status = "Создатель" if admin.status == "creator" else "Админ"
            text += f"• {user.mention_html()} — {status}\n"
        await message.reply(text)
    except Exception as e:
        await message.reply(f"❌ Ошибка: {e}")


@router.message(lambda m: m.text and m.text.lower().startswith((
    "кто модер", ".кто модер", "!кто модер", "ктомодер",
    "модеры", ".модеры", "модераторы", ".модераторы",
    "moders", ".moders", "!moders"
)))
async def cmd_moders(message: types.Message):
    try:
        admins = await message.chat.get_administrators()
        text = "<b>🛡️ Модерация чата:</b>\n\n"
        for admin in admins:
            user = admin.user
            if user.is_bot:
                continue
            rank = await get_user_rank(user.id, message.chat.id)
            rank_name = RANK_NAMES.get(rank, "Участник")
            text += f"• {user.mention_html()} — {rank_name}\n"
        await message.reply(text)
    except Exception as e:
        await message.reply(f"❌ Ошибка: {e}")


@router.message(lambda m: m.text and m.text.lower().startswith((
    "инфо", ".инфо", "!инфо", "/инфо", "info", ".info", "!info"
)))
async def cmd_info(message: types.Message):
    target = await get_target(message)
    if not target:
        target = message.from_user

    user = get_user(target.id, message.chat.id)
    if not user:
        create_user(target.id, message.chat.id, target.username, target.first_name)
        user = get_user(target.id, message.chat.id)

    rank = await get_user_rank(target.id, message.chat.id)
    rank_name = RANK_NAMES.get(rank, "Участник")

    text = (
        f"<b>👤 Информация о пользователе</b>\n\n"
        f"• Имя: {target.full_name}\n"
        f"• Username: @{target.username or 'нет'}\n"
        f"• ID: <code>{target.id}</code>\n"
        f"• Ранг: <b>{rank_name}</b>\n"
        f"• Варнов: <b>{user['warns']}</b>\n"
    )
    await message.reply(text)


@router.message(lambda m: m.text and m.text.lower().startswith((
    "стата", ".стата", "!стата", "/стата", "stats", ".stats", "!stats"
)))
async def cmd_stats(message: types.Message):
    target = await get_target(message)
    if target:
        user = get_user(target.id, message.chat.id)
        if not user:
            await message.reply(f"У {target.mention_html()} нет статистики.")
            return
        rank = await get_user_rank(target.id, message.chat.id)
        text = (
            f"<b>📊 Статистика {target.full_name}</b>\n\n"
            f"• Ранг: <b>{RANK_NAMES.get(rank, 'Участник')}</b>\n"
            f"• Варнов: <b>{user['warns']}</b>\n"
        )
        await message.reply(text)
    else:
        stats = chat_stats[message.chat.id]
        text = (
            f"<b>📊 Статистика чата</b>\n\n"
            f"• Сообщений: <b>{stats['messages']}</b>\n"
            f"• Участников: <b>{len(stats['users'])}</b>\n"
        )
        await message.reply(text)


@router.message(lambda m: m.text and m.text.lower().startswith((
    "топ", ".топ", "!топ", "/топ", "top", ".top", "!top"
)))
async def cmd_top(message: types.Message):
    stats = chat_stats[message.chat.id]
    if not stats["users"]:
        await message.reply("Пока нет статистики.")
        return
    sorted_users = sorted(stats["users"].items(), key=lambda x: x[1], reverse=True)[:10]
    text = "<b>🏆 Топ активных:</b>\n\n"
    for i, (user_id, count) in enumerate(sorted_users, 1):
        try:
            member = await message.chat.get_member(user_id)
            text += f"{i}. {member.user.mention_html()} — {count} сообщений\n"
        except Exception:
            text += f"{i}. ID {user_id} — {count} сообщений\n"
    await message.reply(text)


# ==================== ЭТАП 6: УПРАВЛЕНИЕ РАНГАМИ ====================
@router.message(lambda m: m.text and re.match(
    r"^[!]{1,5}модер(\s|$)", m.text.lower().strip()
))
async def cmd_quick_moder(message: types.Message):
    """
    Быстрое назначение модератора.
    !модер — 1 (мл. модер)
    !!модер — 2 (ст. модер)
    !!!модер — 3 (мл. админ)
    !!!!модер — 4 (ст. админ)
    !!!!!модер — 5 (создатель)
    """
    if not await is_moderator(message, 4):
        await message.reply("❌ Недостаточно прав.")
        return

    # Считаем количество "!"
    exclamations = 0
    for char in message.text:
        if char == "!":
            exclamations += 1
        else:
            break

    rank = min(exclamations, 5)

    target = await get_target(message)
    if not target:
        await message.reply("❌ Ответьте на сообщение или укажите @username.")
        return
    if target.id == OWNER_ID:
        await message.reply("❌ Нельзя менять ранг владельца.")
        return

    set_rank(target.id, message.chat.id, rank)
    await message.reply(f"✅ {target.mention_html()} назначен на ранг <b>{RANK_NAMES[rank]}</b>")


@router.message(lambda m: m.text and m.text.lower().startswith((
    "снять модера", ".снять модера", "!снять модера", "снятьмодера",
    "снять права", ".снять права", "!снять права",
    "разжаловать", ".разжаловать", "!разжаловать",
)))
async def cmd_remove_moder(message: types.Message):
    """Снять все права."""
    if not await is_moderator(message, 4):
        await message.reply("❌ Недостаточно прав.")
        return

    target = await get_target(message)
    if not target:
        await message.reply("❌ Ответьте на сообщение или укажите @username.")
        return
    if target.id == OWNER_ID:
        await message.reply("❌ Нельзя снять права владельца.")
        return

    set_rank(target.id, message.chat.id, 0)
    await message.reply(f"✅ {target.mention_html()} лишён всех прав (ранг: Участник).")


@router.message(lambda m: m.text and m.text.lower().startswith((
    "права", ".права", "!права", "/права", "perms", ".perms", "!perms"
)))
async def cmd_perms(message: types.Message):
    """Показать права пользователя."""
    target = await get_target(message)
    if not target:
        target = message.from_user

    rank = await get_user_rank(target.id, message.chat.id)
    rank_name = RANK_NAMES.get(rank, "Участник")

    text = (
        f"<b>🛡️ Права {target.full_name}</b>\n\n"
        f"• Ранг: <b>{rank_name}</b>\n"
        f"• Может мутить: {'✅' if rank >= 1 else '❌'}\n"
        f"• Может банить: {'✅' if rank >= 2 else '❌'}\n"
        f"• Может повышать: {'✅' if rank >= 3 else '❌'}\n"
        f"• Может понижать: {'✅' if rank >= 4 else '❌'}\n"
    )
    await message.reply(text)


# ==================== УПРАВЛЕНИЕ ФИЛЬТРАМИ ====================
@router.message(lambda m: m.text and m.text.lower().startswith(("+антиссылка", ".антиссылка")))
async def cmd_antilink_on(message: types.Message):
    if not await is_moderator(message, 2):
        return
    chat_filters[message.chat.id]["antilink"] = True
    await message.reply("✅ Антиссылки включены.")


@router.message(lambda m: m.text and m.text.lower().startswith(("-антиссылка", ".антиссылка выкл")))
async def cmd_antilink_off(message: types.Message):
    if not await is_moderator(message, 2):
        return
    chat_filters[message.chat.id]["antilink"] = False
    await message.reply("❌ Антиссылки выключены.")


@router.message(lambda m: m.text and m.text.lower().startswith(("+антимат", ".антимат")))
async def cmd_antimat_on(message: types.Message):
    if not await is_moderator(message, 2):
        return
    chat_filters[message.chat.id]["antimat"] = True
    await message.reply("✅ Антимат включён.")


@router.message(lambda m: m.text and m.text.lower().startswith(("-антимат", ".антимат выкл")))
async def cmd_antimat_off(message: types.Message):
    if not await is_moderator(message, 2):
        return
    chat_filters[message.chat.id]["antimat"] = False
    await message.reply("❌ Антимат выключен.")


@router.message(lambda m: m.text and m.text.lower().startswith(("+антифлуд", ".антифлуд")))
async def cmd_antiflood_on(message: types.Message):
    if not await is_moderator(message, 2):
        return
    chat_filters[message.chat.id]["antiflood"] = True
    await message.reply("✅ Антифлуд включён.")


@router.message(lambda m: m.text and m.text.lower().startswith(("-антифлуд", ".антифлуд выкл")))
async def cmd_antiflood_off(message: types.Message):
    if not await is_moderator(message, 2):
        return
    chat_filters[message.chat.id]["antiflood"] = False
    await message.reply("❌ Антифлуд выключен.")


# ==================== ТРИГГЕРЫ ====================
@router.message(lambda m: m.text and m.text.lower().startswith((
    "триггер варн", ".триггер варн", "!триггер варн"
)))
async def cmd_set_warn_trigger(message: types.Message):
    if not await is_moderator(message, 3):
        await message.reply("❌ Недостаточно прав.")
        return

    args = message.text.split()
    if len(args) < 5:
        await message.reply("Формат: <code>триггер варн 3 мут 1ч</code>")
        return

    try:
        threshold = int(args[2])
    except ValueError:
        await message.reply("❌ Число варнов должно быть числом.")
        return

    action_map = {"мут": "mute", "бан": "ban", "кик": "kick", "варн": "warn"}
    action = action_map.get(args[3].lower())
    if not action:
        await message.reply("❌ Действие: мут, бан, кик, варн")
        return

    time_str = args[4] if len(args) > 4 else "0"
    key = f"warn_{threshold}"
    chat_triggers[message.chat.id][key] = {
        "action": action, "time": time_str, "enabled": True,
    }
    await message.reply(f"✅ Триггер: <b>{threshold}</b> варнов → <b>{args[3]}</b> на <b>{time_str}</b>")


@router.message(lambda m: m.text and m.text.lower().startswith((
    "триггеры", ".триггеры", "!триггеры"
)))
async def cmd_list_triggers(message: types.Message):
    triggers = chat_triggers[message.chat.id]
    text = "<b>📋 Триггеры чата:</b>\n\n"

    for key in ["warn_3", "warn_5", "warn_7", "warn_10"]:
        t = triggers.get(key)
        if t and t.get("enabled"):
            threshold = key.split("_")[1]
            text += f"• {threshold} варнов → {t['action']} на {t['time']}\n"

    if triggers["link_enabled"]:
        text += f"• Ссылка → {triggers['link_action']} на {triggers['link_time']}\n"
    if triggers["mat_enabled"]:
        text += f"• Мат → {triggers['mat_action']} на {triggers['mat_time']}\n"

    await message.reply(text)


# ==================== АВТОФИЛЬТРЫ ====================
# PATCH: earlier catch-all decorator removed so commands and final filters can run.
async def auto_filters(message: types.Message):
    if message.from_user.id == OWNER_ID:
        return

    chat_id = message.chat.id
    chat_stats[chat_id]["messages"] += 1
    chat_stats[chat_id]["users"][message.from_user.id] += 1

    if await has_rank(message, 1):
        return

    filters = chat_filters[chat_id]
    triggers = chat_triggers[chat_id]

    if filters["antibot"] and message.from_user.is_bot:
        try:
            await message.bot.ban_chat_member(chat_id, message.from_user.id)
            await message.answer(f"🤖 Бот {message.from_user.mention_html()} забанен.")
        except Exception:
            pass
        return

    if filters["antisticker"] and message.sticker:
        await apply_filter_punishment(message, "стикер")
        return
    if filters["antigif"] and message.animation:
        await apply_filter_punishment(message, "GIF")
        return
    if filters["antiphoto"] and message.photo:
        await apply_filter_punishment(message, "фото")
        return
    if filters["antivideo"] and message.video:
        await apply_filter_punishment(message, "видео")
        return
    if filters["antivoice"] and message.voice:
        await apply_filter_punishment(message, "голосовое")
        return
    if filters["antidocument"] and message.document:
        await apply_filter_punishment(message, "документ")
        return

    if not message.text:
        return

    text = message.text
    lower_text = text.lower()

    if triggers["link_enabled"] and URL_PATTERN.search(text):
        await apply_punishment(
            message.bot, chat_id, message.from_user.id,
            triggers["link_action"], triggers["link_time"], "ссылка в чате",
        )
        return

    if triggers["mat_enabled"]:
        for word in BAD_WORDS:
            if word in lower_text:
                await apply_punishment(
                    message.bot, chat_id, message.from_user.id,
                    triggers["mat_action"], triggers["mat_time"], "нецензурная лексика",
                )
                return

    if filters["antiall"] and ALL_PATTERN.search(text):
        await apply_filter_punishment(message, "@all")
        return

    if filters["antilink"] and URL_PATTERN.search(text):
        await apply_filter_punishment(message, "ссылка")
        return

    if filters["antiinvite"] and ("t.me/joinchat" in lower_text or "telegram.me/joinchat" in lower_text):
        await apply_filter_punishment(message, "инвайт")
        return

    if filters["antimat"]:
        for word in BAD_WORDS:
            if word in lower_text:
                await apply_filter_punishment(message, "мат")
                return

    if filters["antireklama"]:
        for word in REKLAMA_WORDS:
            if word in lower_text:
                await apply_filter_punishment(message, "реклама")
                return

    if filters["antikripta"]:
        for word in KRIPTA_WORDS:
            if word in lower_text:
                await apply_filter_punishment(message, "крипта")
                return

    if filters["antifishing"]:
        for pattern in FISHING_PATTERNS:
            if re.search(pattern, lower_text):
                await apply_filter_punishment(message, "фишинг")
                return

    if filters["anticaps"] and len(text) > 5:
        letters = [c for c in text if c.isalpha()]
        if letters:
            caps = sum(1 for c in letters if c.isupper())
            percent = (caps / len(letters)) * 100
            if percent >= filters["anticaps_percent"]:
                await apply_filter_punishment(message, "капс")
                return

    if filters["antiflood"]:
        now = datetime.now()
        window = filters["antiflood_window"]
        limit = filters["antiflood_limit"]

        flood_tracker[chat_id][message.from_user.id] = [
            t for t in flood_tracker[chat_id][message.from_user.id]
            if (now - t).total_seconds() < window
        ]
        flood_tracker[chat_id][message.from_user.id].append(now)

        if len(flood_tracker[chat_id][message.from_user.id]) > limit:
            try:
                await message.bot.restrict_chat_member(
                    chat_id=chat_id, user_id=message.from_user.id,
                    permissions=ChatPermissions(can_send_messages=False),
                    until_date=now + timedelta(minutes=5),
                )
                await message.answer(f"🌊 {message.from_user.mention_html()} замучен на 5 минут за флуд.")
                flood_tracker[chat_id][message.from_user.id] = []
            except Exception:
                pass
                """
Модуль модерации. Этап 7: логи, статистика, отчёты.
"""

import re
from datetime import datetime, timedelta
from collections import defaultdict
from aiogram import Router, types
from aiogram.types import ChatPermissions
from database.crud import (
    get_user, create_user, add_warn, reset_warns, get_chat, set_rank,
)
from middlewares.check_rank import has_rank, get_user_rank, OWNER_ID

# PATCH: router reset removed; all handlers share the first router.

RANK_NAMES = {
    0: "Участник",
    1: "Младший модератор",
    2: "Старший модератор",
    3: "Младший администратор",
    4: "Старший администратор",
    5: "Создатель",
}

FULL_PERMISSIONS = ChatPermissions(
    can_send_messages=True, can_send_media_messages=True,
    can_send_other_messages=True, can_add_web_page_previews=True,
    can_send_polls=True, can_change_info=False,
    can_invite_users=True, can_pin_messages=False,
)

# ==================== ХРАНИЛИЩА ====================
chat_filters = defaultdict(lambda: {
    "antilink": False, "antimat": False,
    "antiflood": False, "antiflood_limit": 5, "antiflood_window": 10,
    "anticaps": False, "anticaps_percent": 70,
    "antisticker": False, "antigif": False, "antiphoto": False,
    "antivideo": False, "antivoice": False, "antidocument": False,
    "antibot": False, "antireklama": False, "antikripta": False,
    "antifishing": False, "antiinvite": False, "antiall": False,
    "filter_mute_time": 10,
})

chat_triggers = defaultdict(lambda: {
    "warn_3": {"action": "mute", "time": "1ч", "enabled": True},
    "warn_5": {"action": "mute", "time": "24ч", "enabled": True},
    "warn_7": {"action": "ban", "time": "7д", "enabled": True},
    "warn_10": {"action": "ban", "time": "0", "enabled": True},
    "link_action": "ban", "link_time": "30д", "link_enabled": False,
    "mat_action": "mute", "mat_time": "30м", "mat_enabled": False,
    "warn_limit": 3,
})

flood_tracker = defaultdict(lambda: defaultdict(list))

chat_stats = defaultdict(lambda: {
    "messages": 0, "users": defaultdict(int), "created": datetime.now(),
})

# ==================== ЛОГИ ====================
# Хранилище логов (в памяти)
# В продакшене лучше хранить в Supabase
chat_logs = defaultdict(lambda: {
    "mutes": [],      # {"moderator": id, "target": id, "time": str, "reason": str, "date": datetime}
    "bans": [],       # {"moderator": id, "target": id, "time": str, "reason": str, "date": datetime}
    "warns": [],      # {"moderator": id, "target": id, "reason": str, "date": datetime}
    "kicks": [],      # {"moderator": id, "target": id, "date": datetime}
    "promotes": [],   # {"moderator": id, "target": id, "rank": int, "date": datetime}
    "demotes": [],    # {"moderator": id, "target": id, "rank": int, "date": datetime}
})

# Топ нарушителей
violators = defaultdict(lambda: defaultdict(int))  # {chat_id: {user_id: count}}


def log_action(chat_id: int, action: str, moderator_id: int, target_id: int, **kwargs):
    """Записать действие в лог."""
    entry = {
        "moderator": moderator_id,
        "target": target_id,
        "date": datetime.now(),
    }
    entry.update(kwargs)
    chat_logs[chat_id][action].append(entry)
    # Ограничиваем размер логов
    if len(chat_logs[chat_id][action]) > 500:
        chat_logs[chat_id][action] = chat_logs[chat_id][action][-500:]

    # Считаем нарушителей
    if action in ("mutes", "bans", "warns", "kicks"):
        violators[chat_id][target_id] += 1


# ==================== СПИСКИ ====================
BAD_WORDS = [
    "хуй", "пизд", "бляд", "ебал", "ебат", "сука", "блядь", "мраз",
    "fuck", "shit", "bitch", "asshole", "dick", "pussy",
]

REKLAMA_WORDS = [
    "купить", "продам", "продаю", "скидка", "акция", "распродажа",
    "заработок", "заработать", "инвестиции", "вложить", "крипта",
    "btc", "bitcoin", "eth", "ethereum", "usdt", "toncoin", "ton",
    "казино", "ставки", "букмекер", "1xbet", "betting",
    "подпишись", "подписка", "канал", "чат", "переходи",
    "промокод", "реферал", "рефка", "приглашаю",
]

KRIPTA_WORDS = [
    "btc", "bitcoin", "eth", "ethereum", "usdt", "toncoin", "ton",
    "crypto", "крипта", "кошелёк", "wallet", "binance", "bybit",
    "p2p", "обменник", "майнинг", "mining", "нфт", "nft",
]

FISHING_PATTERNS = [
    r"t\.me/joinchat", r"telegram\.me/joinchat",
    r"bit\.ly", r"tinyurl", r"goo\.gl", r"shorturl",
    r"free.*ton", r"bonus.*ton", r"gift.*ton",
    r"airdrop", r"giveaway", r"claim.*reward",
    r"verify.*wallet", r"connect.*wallet", r"seed.*phrase",
]

URL_PATTERN = re.compile(r"(https?://|www\.|t\.me/|telegram\.me/)", re.IGNORECASE)
ALL_PATTERN = re.compile(r"@(all|everyone|here)", re.IGNORECASE)


# ==================== ПАРСЕР ВРЕМЕНИ ====================
def parse_time(text: str):
    if not text:
        return None
    text = text.lower().strip()

    time_units = {
        "сек": 1, "секунда": 1, "секунды": 1, "секунд": 1, "с": 1,
        "мин": 60, "минута": 60, "минуты": 60, "минут": 60, "м": 60,
        "час": 3600, "часа": 3600, "часов": 3600, "ч": 3600,
        "день": 86400, "дня": 86400, "дней": 86400, "д": 86400, "сутки": 86400,
        "нед": 604800, "неделя": 604800, "недели": 604800, "недель": 604800, "н": 604800,
        "мес": 2592000, "месяц": 2592000, "месяца": 2592000, "месяцев": 2592000,
        "год": 31536000, "года": 31536000, "лет": 31536000, "г": 31536000,
    }

    match = re.match(r"^(\d+)\s*([а-яa-z]+)$", text)
    if match:
        num = int(match.group(1))
        unit = match.group(2)
        for key, multiplier in time_units.items():
            if unit.startswith(key) or key.startswith(unit):
                return timedelta(seconds=num * multiplier)
        return None

    match = re.match(r"^(\d+)", text)
    if not match:
        return None

    num = int(match.group(1))
    rest = text[match.end():].strip()
    for unit, multiplier in time_units.items():
        if unit in rest:
            return timedelta(seconds=num * multiplier)

    return None


async def is_moderator(message: types.Message, required: int = 1) -> bool:
    if message.from_user.id == OWNER_ID:
        return True
    return await has_rank(message, required)


async def get_target(message: types.Message):
    if message.reply_to_message:
        return message.reply_to_message.from_user

    args = message.text.split()
    if len(args) < 2:
        return None

    target_username = args[1].lstrip("@")
    try:
        member = await message.chat.get_member(target_username)
        return member.user
    except Exception:
        return None


def get_duration_arg(message: types.Message):
    if not message.text:
        return None
    args = message.text.split()
    start_index = 1
    if len(args) > 1 and args[1].startswith("@"):
        start_index = 2
    time_parts = args[start_index:]
    if not time_parts:
        return None
    time_str = " ".join(time_parts)
    if not re.search(r"\d", time_str):
        return None
    return time_str


async def apply_filter_punishment(message: types.Message, reason: str):
    chat_id = message.chat.id
    mute_time = chat_filters[chat_id]["filter_mute_time"]
    try:
        await message.delete()
    except Exception:
        pass
    try:
        await message.bot.restrict_chat_member(
            chat_id=chat_id, user_id=message.from_user.id,
            permissions=ChatPermissions(can_send_messages=False),
            until_date=datetime.now() + timedelta(minutes=mute_time),
        )
        log_action(chat_id, "mutes", 0, message.from_user.id, time=f"{mute_time}м", reason=f"фильтр: {reason}")
        await message.answer(f"🚫 {message.from_user.mention_html()} замучен на {mute_time} мин. Причина: {reason}")
    except Exception:
        pass


async def apply_punishment(bot, chat_id: int, user_id: int, action: str, time_str: str, reason: str):
    try:
        if action == "mute":
            duration = parse_time(time_str) if time_str else None
            until = datetime.now() + duration if duration else datetime.now() + timedelta(days=3650)
            await bot.restrict_chat_member(
                chat_id=chat_id, user_id=user_id,
                permissions=ChatPermissions(can_send_messages=False),
                until_date=until,
            )
            log_action(chat_id, "mutes", 0, user_id, time=time_str or "бессрочно", reason=reason)
            await bot.send_message(chat_id, f"🔇 <a href='tg://user?id={user_id}'>Пользователь</a> замучен на {time_str or 'бессрочно'}. Причина: {reason}")
        elif action == "ban":
            await bot.ban_chat_member(chat_id, user_id)
            log_action(chat_id, "bans", 0, user_id, time=time_str or "навсегда", reason=reason)
            await bot.send_message(chat_id, f"🔨 <a href='tg://user?id={user_id}'>Пользователь</a> забанен. Причина: {reason}")
        elif action == "kick":
            await bot.ban_chat_member(chat_id, user_id)
            await bot.unban_chat_member(chat_id, user_id)
            log_action(chat_id, "kicks", 0, user_id)
            await bot.send_message(chat_id, f"👢 <a href='tg://user?id={user_id}'>Пользователь</a> кикнут. Причина: {reason}")
        elif action == "warn":
            warns = add_warn(user_id, chat_id)
            log_action(chat_id, "warns", 0, user_id, reason=reason)
            await bot.send_message(chat_id, f"⚠️ <a href='tg://user?id={user_id}'>Пользователь</a> получил варн. Всего: {warns}. Причина: {reason}")
    except Exception as e:
        print(f"Punishment error: {e}")


# ==================== МУТ ====================
@router.message(lambda m: m.text and m.text.lower().startswith((
    "мут", ".мут", "!мут", "/мут", "mute", ".mute", "!mute", "/mute"
)))
async def cmd_mute(message: types.Message):
    if not await is_moderator(message, 1):
        await message.reply("❌ Недостаточно прав.")
        return

    target = await get_target(message)
    if not target:
        await message.reply("❌ Ответьте на сообщение или укажите @username.")
        return
    if target.id == OWNER_ID:
        await message.reply("❌ Нельзя замутить владельца.")
        return
    if target.id == message.from_user.id:
        await message.reply("❌ Нельзя замутить самого себя.")
        return

    duration, time_text, reason = parse_duration_reason(message)
    until = datetime.now() + duration if duration else datetime.now() + timedelta(days=3650)

    try:
        await message.bot.restrict_chat_member(
            chat_id=message.chat.id, user_id=target.id,
            permissions=ChatPermissions(can_send_messages=False),
            until_date=until,
        )
        log_action(message.chat.id, "mutes", message.from_user.id, target.id, time=time_text, reason=reason)
        try:
            log_action(message.chat.id, "mutes", message.from_user.id, target.id, time=time_text, reason=reason)
        except Exception:
            pass
        await message.reply(f"🔇 {target.mention_html()} замучен на {time_text}. Причина: {reason}.")
    except Exception as e:
        await message.reply(f"❌ Ошибка: {e}")


@router.message(lambda m: m.text and m.text.lower().startswith((
    "размут", ".размут", "!размут", "/размут",
    "унмут", ".унмут", "!унмут", "/унмут",
    "анмут", ".анмут", "!анмут", "/анмут",
    "unmute", ".unmute", "!unmute", "/unmute"
)))
async def cmd_unmute(message: types.Message):
    if not await is_moderator(message, 1):
        await message.reply("❌ Недостаточно прав.")
        return

    target = await get_target(message)
    if not target:
        await message.reply("❌ Ответьте на сообщение или укажите @username.")
        return

    try:
        await message.bot.restrict_chat_member(
            chat_id=message.chat.id, user_id=target.id,
            permissions=FULL_PERMISSIONS,
        )
        await message.reply(f"🔊 {target.mention_html()} размучен.")
    except Exception as e:
        await message.reply(f"❌ Ошибка: {e}")


# ==================== БАН ====================
@router.message(lambda m: m.text and m.text.lower().startswith((
    "бан", ".бан", "!бан", "/бан", "ban", ".ban", "!ban", "/ban"
)))
async def cmd_ban(message: types.Message):
    if not await is_moderator(message, 2):
        await message.reply("❌ Недостаточно прав.")
        return

    target = await get_target(message)
    if not target:
        await message.reply("❌ Ответьте на сообщение или укажите @username.")
        return
    if target.id == OWNER_ID:
        await message.reply("❌ Нельзя забанить владельца.")
        return

    time_arg = get_duration_arg(message)
    duration = parse_time(time_arg) if time_arg else None

    try:
        await message.bot.ban_chat_member(message.chat.id, target.id)
        log_action(message.chat.id, "bans", message.from_user.id, target.id,
                   time=time_arg or "навсегда", reason="ручной бан")
        if duration:
            await message.reply(f"🔨 {target.mention_html()} забанен на {time_arg}.")
        else:
            await message.reply(f"🔨 {target.mention_html()} забанен навсегда.")
    except Exception as e:
        await message.reply(f"❌ Ошибка: {e}")


@router.message(lambda m: m.text and m.text.lower().startswith((
    "разбан", ".разбан", "!разбан", "/разбан",
    "унбан", ".унбан", "!унбан", "/унбан",
    "unban", ".unban", "!unban", "/unban"
)))
async def cmd_unban(message: types.Message):
    if not await is_moderator(message, 2):
        await message.reply("❌ Недостаточно прав.")
        return

    target = await get_target(message)
    if not target:
        await message.reply("❌ Ответьте на сообщение или укажите @username.")
        return

    try:
        await message.bot.unban_chat_member(message.chat.id, target.id)
        await message.reply(f"✅ {target.mention_html()} разбанен.")
    except Exception as e:
        await message.reply(f"❌ Ошибка: {e}")


# ==================== КИК ====================
@router.message(lambda m: m.text and m.text.lower().startswith((
    "кик", ".кик", "!кик", "/кик", "kick", ".kick", "!kick", "/kick"
)))
async def cmd_kick(message: types.Message):
    if not await is_moderator(message, 1):
        await message.reply("❌ Недостаточно прав.")
        return

    target = await get_target(message)
    if not target:
        await message.reply("❌ Ответьте на сообщение или укажите @username.")
        return
    if target.id == OWNER_ID:
        await message.reply("❌ Нельзя кикнуть владельца.")
        return

    try:
        await message.bot.ban_chat_member(message.chat.id, target.id)
        await message.bot.unban_chat_member(message.chat.id, target.id)
        log_action(message.chat.id, "kicks", message.from_user.id, target.id)
        await message.reply(f"👢 {target.mention_html()} кикнут.")
    except Exception as e:
        await message.reply(f"❌ Ошибка: {e}")


# ==================== ВАРН ====================
@router.message(lambda m: m.text and m.text.lower().startswith((
    "варн", ".варн", "!варн", "/варн",
    "пред", ".пред", "!пред", "/пред",
    "warn", ".warn", "!warn", "/warn"
)))
async def cmd_warn(message: types.Message):
    if not await is_moderator(message, 1):
        await message.reply("❌ Недостаточно прав.")
        return

    target = await get_target(message)
    if not target:
        await message.reply("❌ Ответьте на сообщение или укажите @username.")
        return
    if target.id == OWNER_ID:
        await message.reply("❌ Нельзя выдать варн владельцу.")
        return

    warns = add_warn(target.id, message.chat.id)
    if warns is None:
        create_user(target.id, message.chat.id, target.username, target.first_name)
        warns = add_warn(target.id, message.chat.id)

    log_action(message.chat.id, "warns", message.from_user.id, target.id, reason="ручной варн")
    await message.reply(f"⚠️ {target.mention_html()} получил варн. Всего: {warns}")

    triggers = chat_triggers[message.chat.id]
    for trigger_key in ["warn_3", "warn_5", "warn_7", "warn_10"]:
        trigger = triggers.get(trigger_key)
        if not trigger or not trigger.get("enabled"):
            continue
        threshold = int(trigger_key.split("_")[1])
        if warns == threshold:
            await apply_punishment(
                message.bot, message.chat.id, target.id,
                trigger["action"], trigger["time"],
                f"достигнут лимит варнов ({threshold})",
            )
            reset_warns(target.id, message.chat.id)
            break


@router.message(lambda m: m.text and m.text.lower().startswith((
    "унварн", ".унварн", "!унварн", "/унварн",
    "анварн", ".анварн", "!анварн", "/анварн",
    "снятьварн", ".снятьварн", "!снятьварн", "/снятьварн",
    "unwarn", ".unwarn", "!unwarn", "/unwarn"
)))
async def cmd_unwarn(message: types.Message):
    if not await is_moderator(message, 2):
        await message.reply("❌ Недостаточно прав.")
        return

    target = await get_target(message)
    if not target:
        await message.reply("❌ Ответьте на сообщение или укажите @username.")
        return

    reset_warns(target.id, message.chat.id)
    await message.reply(f"✅ Варны {target.mention_html()} сняты.")


@router.message(lambda m: m.text and m.text.lower().startswith((
    "варны", ".варны", "!варны", "/варны",
    "преды", ".преды", "!преды", "/преды",
    "warns", ".warns", "!warns", "/warns"
)))
async def cmd_warns(message: types.Message):
    target = await get_target(message)
    if not target:
        target = message.from_user

    user = get_user(target.id, message.chat.id)
    if not user:
        await message.reply(f"У {target.mention_html()} нет предупреждений.")
        return

    await message.reply(f"⚠️ {target.mention_html()}: <b>{user['warns']}</b> предупреждений.")


@router.message(lambda m: m.text and m.text.lower().startswith((
    "повысить", ".повысить", "!повысить", "/повысить",
    "promote", ".promote", "!promote", "/promote"
)))
async def cmd_promote(message: types.Message):
    if not await is_moderator(message, 3):
        await message.reply("❌ Недостаточно прав.")
        return

    target = await get_target(message)
    if not target:
        await message.reply("❌ Ответьте на сообщение или укажите @username.")
        return

    current = await get_user_rank(target.id, message.chat.id)
    if current >= 5:
        await message.reply("Максимальный ранг.")
        return

    set_rank(target.id, message.chat.id, current + 1)
    log_action(message.chat.id, "promotes", message.from_user.id, target.id, rank=current + 1)
    await message.reply(f"⬆️ {target.mention_html()} повышен до <b>{RANK_NAMES[current + 1]}</b>")


@router.message(lambda m: m.text and m.text.lower().startswith((
    "понизить", ".понизить", "!понизить", "/понизить",
    "demote", ".demote", "!demote", "/demote"
)))
async def cmd_demote(message: types.Message):
    if not await is_moderator(message, 4):
        await message.reply("❌ Недостаточно прав.")
        return

    target = await get_target(message)
    if not target:
        await message.reply("❌ Ответьте на сообщение или укажите @username.")
        return

    current = await get_user_rank(target.id, message.chat.id)
    if current <= 0:
        return

    set_rank(target.id, message.chat.id, current - 1)
    log_action(message.chat.id, "demotes", message.from_user.id, target.id, rank=current - 1)
    await message.reply(f"⬇️ {target.mention_html()} понижен до <b>{RANK_NAMES[current - 1]}</b>")


@router.message(lambda m: m.text and m.text.lower().startswith((
    "мойранг", ".мойранг", "!мойранг", "/мойранг",
    "rank", ".rank", "!rank", "/rank",
    "ктоя", ".ктоя", "!ктоя", "/ктоя"
)))
async def cmd_rank(message: types.Message):
    target = message.reply_to_message.from_user if message.reply_to_message else message.from_user
    rank = await get_user_rank(target.id, message.chat.id)
    await message.reply(f"Ранг {target.mention_html()}: <b>{RANK_NAMES.get(rank, 'Участник')}</b>")


# ==================== ЭТАП 5: УПРАВЛЕНИЕ СООБЩЕНИЯМИ ====================
@router.message(lambda m: m.text and m.text.lower().startswith((
    "-смс", ".смс", "!смс", "/смс", "del", ".del", "!del", "/del"
)))
async def cmd_delete(message: types.Message):
    if not await is_moderator(message, 1):
        await message.reply("❌ Недостаточно прав.")
        return
    if not message.reply_to_message:
        await message.reply("❌ Ответьте на сообщение, которое нужно удалить.")
        return
    try:
        await message.reply_to_message.delete()
        await message.delete()
    except Exception as e:
        await message.reply(f"❌ Ошибка: {e}")


@router.message(lambda m: m.text and m.text.lower().startswith((
    ".пин", "!пин", "/пин", "pin", ".pin", "!pin", "/pin"
)))
async def cmd_pin(message: types.Message):
    if not await is_moderator(message, 2):
        await message.reply("❌ Недостаточно прав.")
        return
    if not message.reply_to_message:
        await message.reply("❌ Ответьте на сообщение для закрепления.")
        return
    try:
        await message.bot.pin_chat_message(
            chat_id=message.chat.id,
            message_id=message.reply_to_message.message_id,
        )
        await message.reply("📌 Закреплено.")
    except Exception as e:
        await message.reply(f"❌ Ошибка: {e}")


@router.message(lambda m: m.text and m.text.lower().startswith((
    ".анпин", "!анпин", "/анпин", "unpin", ".unpin", "!unpin", "/unpin"
)))
async def cmd_unpin(message: types.Message):
    if not await is_moderator(message, 2):
        await message.reply("❌ Недостаточно прав.")
        return
    try:
        await message.bot.unpin_chat_message(chat_id=message.chat.id)
        await message.reply("📌 Откреплено.")
    except Exception as e:
        await message.reply(f"❌ Ошибка: {e}")


@router.message(lambda m: m.text and m.text.lower().startswith((
    "очистить", ".очистить", "!очистить", "/очистить", "clear", ".clear"
)))
async def cmd_clear(message: types.Message):
    if not await is_moderator(message, 2):
        await message.reply("❌ Недостаточно прав.")
        return

    args = message.text.split()
    if len(args) < 2 or not args[1].isdigit():
        await message.reply("❌ Формат: <code>очистить 10</code>")
        return

    count = min(int(args[1]), 100)
    deleted = 0
    try:
        for offset in range(1, count + 1):
            try:
                await message.bot.delete_message(
                    chat_id=message.chat.id,
                    message_id=message.message_id - offset,
                )
                deleted += 1
            except Exception:
                pass
        await message.answer(f"🧹 Удалено сообщений: {deleted}.")
    except Exception as e:
        await message.reply(f"❌ Ошибка: {e}")


# ==================== ЭТАП 5: ИНФОРМАЦИЯ ====================
@router.message(lambda m: m.text and m.text.lower().startswith((
    "кто админ", ".кто админ", "!кто админ", "ктоадмин", "ктоадмины",
    "admins", ".admins", "!admins"
)))
async def cmd_admins(message: types.Message):
    try:
        admins = await message.chat.get_administrators()
        text = "<b>👑 Администраторы чата:</b>\n\n"
        for admin in admins:
            user = admin.user
            if user.is_bot:
                continue
            status = "Создатель" if admin.status == "creator" else "Админ"
            text += f"• {user.mention_html()} — {status}\n"
        await message.reply(text)
    except Exception as e:
        await message.reply(f"❌ Ошибка: {e}")


@router.message(lambda m: m.text and m.text.lower().startswith((
    "кто модер", ".кто модер", "!кто модер", "ктомодер",
    "модеры", ".модеры", "модераторы", ".модераторы",
    "moders", ".moders", "!moders"
)))
async def cmd_moders(message: types.Message):
    try:
        admins = await message.chat.get_administrators()
        text = "<b>🛡️ Модерация чата:</b>\n\n"
        for admin in admins:
            user = admin.user
            if user.is_bot:
                continue
            rank = await get_user_rank(user.id, message.chat.id)
            rank_name = RANK_NAMES.get(rank, "Участник")
            text += f"• {user.mention_html()} — {rank_name}\n"
        await message.reply(text)
    except Exception as e:
        await message.reply(f"❌ Ошибка: {e}")


@router.message(lambda m: m.text and m.text.lower().startswith((
    "инфо", ".инфо", "!инфо", "/инфо", "info", ".info", "!info"
)))
async def cmd_info(message: types.Message):
    target = await get_target(message)
    if not target:
        target = message.from_user

    user = get_user(target.id, message.chat.id)
    if not user:
        create_user(target.id, message.chat.id, target.username, target.first_name)
        user = get_user(target.id, message.chat.id)

    rank = await get_user_rank(target.id, message.chat.id)
    rank_name = RANK_NAMES.get(rank, "Участник")

    text = (
        f"<b>👤 Информация о пользователе</b>\n\n"
        f"• Имя: {target.full_name}\n"
        f"• Username: @{target.username or 'нет'}\n"
        f"• ID: <code>{target.id}</code>\n"
        f"• Ранг: <b>{rank_name}</b>\n"
        f"• Варнов: <b>{user['warns']}</b>\n"
    )
    await message.reply(text)


@router.message(lambda m: m.text and m.text.lower().startswith((
    "стата", ".стата", "!стата", "/стата", "stats", ".stats", "!stats"
)))
async def cmd_stats(message: types.Message):
    target = await get_target(message)
    if target:
        user = get_user(target.id, message.chat.id)
        if not user:
            await message.reply(f"У {target.mention_html()} нет статистики.")
            return
        rank = await get_user_rank(target.id, message.chat.id)
        text = (
            f"<b>📊 Статистика {target.full_name}</b>\n\n"
            f"• Ранг: <b>{RANK_NAMES.get(rank, 'Участник')}</b>\n"
            f"• Варнов: <b>{user['warns']}</b>\n"
        )
        await message.reply(text)
    else:
        stats = chat_stats[message.chat.id]
        text = (
            f"<b>📊 Статистика чата</b>\n\n"
            f"• Сообщений: <b>{stats['messages']}</b>\n"
            f"• Участников: <b>{len(stats['users'])}</b>\n"
        )
        await message.reply(text)


@router.message(lambda m: m.text and m.text.lower().startswith((
    "топ", ".топ", "!топ", "/топ", "top", ".top", "!top"
)))
async def cmd_top(message: types.Message):
    stats = chat_stats[message.chat.id]
    if not stats["users"]:
        await message.reply("Пока нет статистики.")
        return
    sorted_users = sorted(stats["users"].items(), key=lambda x: x[1], reverse=True)[:10]
    text = "<b>🏆 Топ активных:</b>\n\n"
    for i, (user_id, count) in enumerate(sorted_users, 1):
        try:
            member = await message.chat.get_member(user_id)
            text += f"{i}. {member.user.mention_html()} — {count} сообщений\n"
        except Exception:
            text += f"{i}. ID {user_id} — {count} сообщений\n"
    await message.reply(text)


# ==================== ЭТАП 6: УПРАВЛЕНИЕ РАНГАМИ ====================
@router.message(lambda m: m.text and re.match(
    r"^[!]{1,5}модер(\s|$)", m.text.lower().strip()
))
async def cmd_quick_moder(message: types.Message):
    if not await is_moderator(message, 4):
        await message.reply("❌ Недостаточно прав.")
        return

    exclamations = 0
    for char in message.text:
        if char == "!":
            exclamations += 1
        else:
            break

    rank = min(exclamations, 5)

    target = await get_target(message)
    if not target:
        await message.reply("❌ Ответьте на сообщение или укажите @username.")
        return
    if target.id == OWNER_ID:
        await message.reply("❌ Нельзя менять ранг владельца.")
        return

    set_rank(target.id, message.chat.id, rank)
    log_action(message.chat.id, "promotes", message.from_user.id, target.id, rank=rank)
    await message.reply(f"✅ {target.mention_html()} назначен на ранг <b>{RANK_NAMES[rank]}</b>")


@router.message(lambda m: m.text and m.text.lower().startswith((
    "снять модера", ".снять модера", "!снять модера", "снятьмодера",
    "снять права", ".снять права", "!снять права",
    "разжаловать", ".разжаловать", "!разжаловать",
)))
async def cmd_remove_moder(message: types.Message):
    if not await is_moderator(message, 4):
        await message.reply("❌ Недостаточно прав.")
        return

    target = await get_target(message)
    if not target:
        await message.reply("❌ Ответьте на сообщение или укажите @username.")
        return
    if target.id == OWNER_ID:
        await message.reply("❌ Нельзя снять права владельца.")
        return

    set_rank(target.id, message.chat.id, 0)
    log_action(message.chat.id, "demotes", message.from_user.id, target.id, rank=0)
    await message.reply(f"✅ {target.mention_html()} лишён всех прав.")


@router.message(lambda m: m.text and m.text.lower().startswith((
    "права", ".права", "!права", "/права", "perms", ".perms", "!perms"
)))
async def cmd_perms(message: types.Message):
    target = await get_target(message)
    if not target:
        target = message.from_user

    rank = await get_user_rank(target.id, message.chat.id)
    rank_name = RANK_NAMES.get(rank, "Участник")

    text = (
        f"<b>🛡️ Права {target.full_name}</b>\n\n"
        f"• Ранг: <b>{rank_name}</b>\n"
        f"• Может мутить: {'✅' if rank >= 1 else '❌'}\n"
        f"• Может банить: {'✅' if rank >= 2 else '❌'}\n"
        f"• Может повышать: {'✅' if rank >= 3 else '❌'}\n"
        f"• Может понижать: {'✅' if rank >= 4 else '❌'}\n"
    )
    await message.reply(text)


# ==================== ЭТАП 7: ЛОГИ ====================
@router.message(lambda m: m.text and m.text.lower().startswith((
    "логи", ".логи", "!логи", "/логи", "logs", ".logs", "!logs"
)))
async def cmd_logs(message: types.Message):
    """Показать последние логи действий."""
    if not await is_moderator(message, 3):
        await message.reply("❌ Недостаточно прав.")
        return

    logs = chat_logs[message.chat.id]
    text = "<b>📋 Последние логи:</b>\n\n"

    # Последние 5 мутов
    if logs["mutes"]:
        text += "<b>🔇 Муты:</b>\n"
        for entry in logs["mutes"][-5:]:
            text += (
                f"• <a href='tg://user?id={entry['target']}'>Цель</a> — "
                f"{entry.get('time', '?')} — "
                f"мод: <a href='tg://user?id={entry['moderator']}'>ID{entry['moderator']}</a>\n"
            )
        text += "\n"

    # Последние 5 банов
    if logs["bans"]:
        text += "<b>🔨 Баны:</b>\n"
        for entry in logs["bans"][-5:]:
            text += (
                f"• <a href='tg://user?id={entry['target']}'>Цель</a> — "
                f"{entry.get('time', '?')} — "
                f"мод: <a href='tg://user?id={entry['moderator']}'>ID{entry['moderator']}</a>\n"
            )
        text += "\n"

    # Последние 5 варнов
    if logs["warns"]:
        text += "<b>⚠️ Варны:</b>\n"
        for entry in logs["warns"][-5:]:
            text += (
                f"• <a href='tg://user?id={entry['target']}'>Цель</a> — "
                f"мод: <a href='tg://user?id={entry['moderator']}'>ID{entry['moderator']}</a>\n"
            )
        text += "\n"

    # Последние 5 повышений
    if logs["promotes"]:
        text += "<b>⬆️ Повышения:</b>\n"
        for entry in logs["promotes"][-5:]:
            text += (
                f"• <a href='tg://user?id={entry['target']}'>Цель</a> — "
                f"ранг {entry.get('rank', '?')}\n"
            )

    if text == "<b>📋 Последние логи:</b>\n\n":
        text += "Пока нет записей."

    await message.reply(text)


@router.message(lambda m: m.text and m.text.lower().startswith((
    "топ нарушителей", ".топ нарушителей", "!топ нарушителей", "топнарушителей"
)))
async def cmd_top_violators(message: types.Message):
    """Топ нарушителей чата."""
    v = violators[message.chat.id]
    if not v:
        await message.reply("Пока нет нарушителей.")
        return

    sorted_v = sorted(v.items(), key=lambda x: x[1], reverse=True)[:10]
    text = "<b>🚨 Топ нарушителей:</b>\n\n"
    for i, (user_id, count) in enumerate(sorted_v, 1):
        try:
            member = await message.chat.get_member(user_id)
            text += f"{i}. {member.user.mention_html()} — {count} нарушений\n"
        except Exception:
            text += f"{i}. ID {user_id} — {count} нарушений\n"
    await message.reply(text)


@router.message(lambda m: m.text and m.text.lower().startswith((
    "отчёт", ".отчёт", "!отчёт", "/отчёт", "отчет", ".отчет"
)))
async def cmd_report(message: types.Message):
    """Ежедневный отчёт по чату."""
    if not await is_moderator(message, 3):
        await message.reply("❌ Недостаточно прав.")
        return

    logs = chat_logs[message.chat.id]
    stats = chat_stats[message.chat.id]

    text = (
        f"<b>📊 Отчёт по чату</b>\n\n"
        f"• Сообщений: <b>{stats['messages']}</b>\n"
        f"• Участников: <b>{len(stats['users'])}</b>\n\n"
        f"<b>Действия модерации:</b>\n"
        f"• Мутов: <b>{len(logs['mutes'])}</b>\n"
        f"• Банов: <b>{len(logs['bans'])}</b>\n"
        f"• Варнов: <b>{len(logs['warns'])}</b>\n"
        f"• Киков: <b>{len(logs['kicks'])}</b>\n"
        f"• Повышений: <b>{len(logs['promotes'])}</b>\n"
        f"• Понижений: <b>{len(logs['demotes'])}</b>\n"
    )
    await message.reply(text)


# ==================== УПРАВЛЕНИЕ ФИЛЬТРАМИ ====================
@router.message(lambda m: m.text and m.text.lower().startswith(("+антиссылка", ".антиссылка")))
async def cmd_antilink_on(message: types.Message):
    if not await is_moderator(message, 2):
        return
    chat_filters[message.chat.id]["antilink"] = True
    await message.reply("✅ Антиссылки включены.")


@router.message(lambda m: m.text and m.text.lower().startswith(("-антиссылка", ".антиссылка выкл")))
async def cmd_antilink_off(message: types.Message):
    if not await is_moderator(message, 2):
        return
    chat_filters[message.chat.id]["antilink"] = False
    await message.reply("❌ Антиссылки выключены.")


@router.message(lambda m: m.text and m.text.lower().startswith(("+антимат", ".антимат")))
async def cmd_antimat_on(message: types.Message):
    if not await is_moderator(message, 2):
        return
    chat_filters[message.chat.id]["antimat"] = True
    await message.reply("✅ Антимат включён.")


@router.message(lambda m: m.text and m.text.lower().startswith(("-антимат", ".антимат выкл")))
async def cmd_antimat_off(message: types.Message):
    if not await is_moderator(message, 2):
        return
    chat_filters[message.chat.id]["antimat"] = False
    await message.reply("❌ Антимат выключен.")


@router.message(lambda m: m.text and m.text.lower().startswith(("+антифлуд", ".антифлуд")))
async def cmd_antiflood_on(message: types.Message):
    if not await is_moderator(message, 2):
        return
    chat_filters[message.chat.id]["antiflood"] = True
    await message.reply("✅ Антифлуд включён.")


@router.message(lambda m: m.text and m.text.lower().startswith(("-антифлуд", ".антифлуд выкл")))
async def cmd_antiflood_off(message: types.Message):
    if not await is_moderator(message, 2):
        return
    chat_filters[message.chat.id]["antiflood"] = False
    await message.reply("❌ Антифлуд выключен.")


# ==================== ТРИГГЕРЫ ====================
@router.message(lambda m: m.text and m.text.lower().startswith((
    "триггер варн", ".триггер варн", "!триггер варн"
)))
async def cmd_set_warn_trigger(message: types.Message):
    if not await is_moderator(message, 3):
        await message.reply("❌ Недостаточно прав.")
        return

    args = message.text.split()
    if len(args) < 5:
        await message.reply("Формат: <code>триггер варн 3 мут 1ч</code>")
        return

    try:
        threshold = int(args[2])
    except ValueError:
        await message.reply("❌ Число варнов должно быть числом.")
        return

    action_map = {"мут": "mute", "бан": "ban", "кик": "kick", "варн": "warn"}
    action = action_map.get(args[3].lower())
    if not action:
        await message.reply("❌ Действие: мут, бан, кик, варн")
        return

    time_str = args[4] if len(args) > 4 else "0"
    key = f"warn_{threshold}"
    chat_triggers[message.chat.id][key] = {
        "action": action, "time": time_str, "enabled": True,
    }
    await message.reply(f"✅ Триггер: <b>{threshold}</b> варнов → <b>{args[3]}</b> на <b>{time_str}</b>")


@router.message(lambda m: m.text and m.text.lower().startswith((
    "триггеры", ".триггеры", "!триггеры"
)))
async def cmd_list_triggers(message: types.Message):
    triggers = chat_triggers[message.chat.id]
    text = "<b>📋 Триггеры чата:</b>\n\n"

    for key in ["warn_3", "warn_5", "warn_7", "warn_10"]:
        t = triggers.get(key)
        if t and t.get("enabled"):
            threshold = key.split("_")[1]
            text += f"• {threshold} варнов → {t['action']} на {t['time']}\n"

    if triggers["link_enabled"]:
        text += f"• Ссылка → {triggers['link_action']} на {triggers['link_time']}\n"
    if triggers["mat_enabled"]:
        text += f"• Мат → {triggers['mat_action']} на {triggers['mat_time']}\n"

    await message.reply(text)


# ==================== АВТОФИЛЬТРЫ ====================
# PATCH: earlier catch-all decorator removed so commands and final filters can run.
async def auto_filters(message: types.Message):
    if message.from_user.id == OWNER_ID:
        return

    chat_id = message.chat.id
    chat_stats[chat_id]["messages"] += 1
    chat_stats[chat_id]["users"][message.from_user.id] += 1

    if await has_rank(message, 1):
        return

    filters = chat_filters[chat_id]
    triggers = chat_triggers[chat_id]

    if filters["antibot"] and message.from_user.is_bot:
        try:
            await message.bot.ban_chat_member(chat_id, message.from_user.id)
            log_action(chat_id, "bans", 0, message.from_user.id, time="навсегда", reason="антибот")
            await message.answer(f"🤖 Бот {message.from_user.mention_html()} забанен.")
        except Exception:
            pass
        return

    if filters["antisticker"] and message.sticker:
        await apply_filter_punishment(message, "стикер")
        return
    if filters["antigif"] and message.animation:
        await apply_filter_punishment(message, "GIF")
        return
    if filters["antiphoto"] and message.photo:
        await apply_filter_punishment(message, "фото")
        return
    if filters["antivideo"] and message.video:
        await apply_filter_punishment(message, "видео")
        return
    if filters["antivoice"] and message.voice:
        await apply_filter_punishment(message, "голосовое")
        return
    if filters["antidocument"] and message.document:
        await apply_filter_punishment(message, "документ")
        return

    if not message.text:
        return

    text = message.text
    lower_text = text.lower()

    if triggers["link_enabled"] and URL_PATTERN.search(text):
        await apply_punishment(
            message.bot, chat_id, message.from_user.id,
            triggers["link_action"], triggers["link_time"], "ссылка в чате",
        )
        return

    if triggers["mat_enabled"]:
        for word in BAD_WORDS:
            if word in lower_text:
                await apply_punishment(
                    message.bot, chat_id, message.from_user.id,
                    triggers["mat_action"], triggers["mat_time"], "нецензурная лексика",
                )
                return

    if filters["antiall"] and ALL_PATTERN.search(text):
        await apply_filter_punishment(message, "@all")
        return

    if filters["antilink"] and URL_PATTERN.search(text):
        await apply_filter_punishment(message, "ссылка")
        return

    if filters["antiinvite"] and ("t.me/joinchat" in lower_text or "telegram.me/joinchat" in lower_text):
        await apply_filter_punishment(message, "инвайт")
        return

    if filters["antimat"]:
        for word in BAD_WORDS:
            if word in lower_text:
                await apply_filter_punishment(message, "мат")
                return

    if filters["antireklama"]:
        for word in REKLAMA_WORDS:
            if word in lower_text:
                await apply_filter_punishment(message, "реклама")
                return

    if filters["antikripta"]:
        for word in KRIPTA_WORDS:
            if word in lower_text:
                await apply_filter_punishment(message, "крипта")
                return

    if filters["antifishing"]:
        for pattern in FISHING_PATTERNS:
            if re.search(pattern, lower_text):
                await apply_filter_punishment(message, "фишинг")
                return

    if filters["anticaps"] and len(text) > 5:
        letters = [c for c in text if c.isalpha()]
        if letters:
            caps = sum(1 for c in letters if c.isupper())
            percent = (caps / len(letters)) * 100
            if percent >= filters["anticaps_percent"]:
                await apply_filter_punishment(message, "капс")
                return

    if filters["antiflood"]:
        now = datetime.now()
        window = filters["antiflood_window"]
        limit = filters["antiflood_limit"]

        flood_tracker[chat_id][message.from_user.id] = [
            t for t in flood_tracker[chat_id][message.from_user.id]
            if (now - t).total_seconds() < window
        ]
        flood_tracker[chat_id][message.from_user.id].append(now)

        if len(flood_tracker[chat_id][message.from_user.id]) > limit:
            try:
                await message.bot.restrict_chat_member(
                    chat_id=chat_id, user_id=message.from_user.id,
                    permissions=ChatPermissions(can_send_messages=False),
                    until_date=now + timedelta(minutes=5),
                )
                log_action(chat_id, "mutes", 0, message.from_user.id, time="5м", reason="флуд")
                await message.answer(f"🌊 {message.from_user.mention_html()} замучен на 5 минут за флуд.")
                flood_tracker[chat_id][message.from_user.id] = []
            except Exception:
                pass
                """
Модуль модерации. Этап 8 (финальный): глобальная модерация + сетка.
"""

import re
from datetime import datetime, timedelta
from collections import defaultdict
from aiogram import Router, types
from aiogram.types import ChatPermissions
from database.crud import (
    get_user, create_user, add_warn, reset_warns, get_chat, set_rank,
    add_global_ban, remove_global_ban, is_globally_banned,
    add_global_chat, remove_global_chat, get_global_chats,
)
from middlewares.check_rank import has_rank, get_user_rank, OWNER_ID

# PATCH: router reset removed; all handlers share the first router.

RANK_NAMES = {
    0: "Участник", 1: "Младший модератор", 2: "Старший модератор",
    3: "Младший администратор", 4: "Старший администратор", 5: "Создатель",
}

FULL_PERMISSIONS = ChatPermissions(
    can_send_messages=True, can_send_media_messages=True,
    can_send_other_messages=True, can_add_web_page_previews=True,
    can_send_polls=True, can_change_info=False,
    can_invite_users=True, can_pin_messages=False,
)

chat_filters = defaultdict(lambda: {
    "antilink": False, "antimat": False,
    "antiflood": False, "antiflood_limit": 5, "antiflood_window": 10,
    "anticaps": False, "anticaps_percent": 70,
    "antisticker": False, "antigif": False, "antiphoto": False,
    "antivideo": False, "antivoice": False, "antidocument": False,
    "antibot": False, "antireklama": False, "antikripta": False,
    "antifishing": False, "antiinvite": False, "antiall": False,
    "filter_mute_time": 10,
})

chat_triggers = defaultdict(lambda: {
    "warn_3": {"action": "mute", "time": "1ч", "enabled": True},
    "warn_5": {"action": "mute", "time": "24ч", "enabled": True},
    "warn_7": {"action": "ban", "time": "7д", "enabled": True},
    "warn_10": {"action": "ban", "time": "0", "enabled": True},
    "link_action": "ban", "link_time": "30д", "link_enabled": False,
    "mat_action": "mute", "mat_time": "30м", "mat_enabled": False,
    "warn_limit": 3,
})

flood_tracker = defaultdict(lambda: defaultdict(list))
chat_stats = defaultdict(lambda: {"messages": 0, "users": defaultdict(int), "created": datetime.now()})
chat_logs = defaultdict(lambda: {
    "mutes": [], "bans": [], "warns": [], "kicks": [], "promotes": [], "demotes": [],
})
violators = defaultdict(lambda: defaultdict(int))

BAD_WORDS = ["хуй", "пизд", "бляд", "ебал", "ебат", "сука", "блядь", "мраз",
             "fuck", "shit", "bitch", "asshole", "dick", "pussy"]
REKLAMA_WORDS = ["купить", "продам", "продаю", "скидка", "акция", "распродажа",
                 "заработок", "заработать", "инвестиции", "вложить", "крипта",
                 "btc", "bitcoin", "eth", "ethereum", "usdt", "toncoin", "ton",
                 "казино", "ставки", "букмекер", "1xbet", "подпишись", "канал",
                 "промокод", "реферал", "рефка", "приглашаю"]
KRIPTA_WORDS = ["btc", "bitcoin", "eth", "ethereum", "usdt", "toncoin", "ton",
                "crypto", "крипта", "кошелёк", "wallet", "binance", "bybit",
                "p2p", "обменник", "майнинг", "nft"]
FISHING_PATTERNS = [r"t\.me/joinchat", r"telegram\.me/joinchat", r"bit\.ly",
                    r"tinyurl", r"goo\.gl", r"airdrop", r"giveaway",
                    r"verify.*wallet", r"seed.*phrase"]
URL_PATTERN = re.compile(r"(https?://|www\.|t\.me/|telegram\.me/)", re.IGNORECASE)
ALL_PATTERN = re.compile(r"@(all|everyone|here)", re.IGNORECASE)


def log_action(chat_id, action, moderator_id, target_id, **kwargs):
    entry = {"moderator": moderator_id, "target": target_id, "date": datetime.now()}
    entry.update(kwargs)
    chat_logs[chat_id][action].append(entry)
    if len(chat_logs[chat_id][action]) > 500:
        chat_logs[chat_id][action] = chat_logs[chat_id][action][-500:]
    if action in ("mutes", "bans", "warns", "kicks"):
        violators[chat_id][target_id] += 1


def parse_time(text):
    if not text:
        return None
    text = text.lower().strip()
    units = {
        "сек": 1, "с": 1, "секунда": 1, "секунды": 1, "секунд": 1,
        "мин": 60, "м": 60, "минута": 60, "минуты": 60, "минут": 60,
        "час": 3600, "ч": 3600, "часа": 3600, "часов": 3600,
        "день": 86400, "д": 86400, "дня": 86400, "дней": 86400, "сутки": 86400,
        "нед": 604800, "н": 604800, "неделя": 604800, "недели": 604800, "недель": 604800,
        "мес": 2592000, "месяц": 2592000, "месяца": 2592000, "месяцев": 2592000,
        "год": 31536000, "г": 31536000, "года": 31536000, "лет": 31536000,
    }
    match = re.match(r"^(\d+)\s*([а-яa-z]+)$", text)
    if match:
        num = int(match.group(1))
        unit = match.group(2)
        for key, mult in units.items():
            if unit.startswith(key) or key.startswith(unit):
                return timedelta(seconds=num * mult)
        return None
    match = re.match(r"^(\d+)", text)
    if not match:
        return None
    num = int(match.group(1))
    rest = text[match.end():].strip()
    for unit, mult in units.items():
        if unit in rest:
            return timedelta(seconds=num * mult)
    return None


async def is_moderator(message, required=1):
    if message.from_user.id == OWNER_ID:
        return True
    return await has_rank(message, required)


async def get_target(message):
    if message.reply_to_message:
        return message.reply_to_message.from_user
    args = message.text.split()
    if len(args) < 2:
        return None
    target_username = args[1].lstrip("@")
    try:
        member = await message.chat.get_member(target_username)
        return member.user
    except Exception:
        return None


def get_duration_arg(message):
    if not message.text:
        return None
    args = message.text.split()
    start = 1
    if len(args) > 1 and args[1].startswith("@"):
        start = 2
    parts = args[start:]
    if not parts:
        return None
    time_str = " ".join(parts)
    if not re.search(r"\d", time_str):
        return None
    return time_str


async def apply_filter_punishment(message, reason):
    chat_id = message.chat.id
    mute_time = chat_filters[chat_id]["filter_mute_time"]
    try:
        await message.delete()
    except Exception:
        pass
    try:
        await message.bot.restrict_chat_member(
            chat_id=chat_id, user_id=message.from_user.id,
            permissions=ChatPermissions(can_send_messages=False),
            until_date=datetime.now() + timedelta(minutes=mute_time),
        )
        log_action(chat_id, "mutes", 0, message.from_user.id, time=f"{mute_time}м", reason=f"фильтр: {reason}")
        await message.answer(f"🚫 {message.from_user.mention_html()} замучен на {mute_time} мин. Причина: {reason}")
    except Exception:
        pass


async def apply_punishment(bot, chat_id, user_id, action, time_str, reason):
    try:
        if action == "mute":
            dur = parse_time(time_str) if time_str else None
            until = datetime.now() + dur if dur else datetime.now() + timedelta(days=3650)
            await bot.restrict_chat_member(
                chat_id=chat_id, user_id=user_id,
                permissions=ChatPermissions(can_send_messages=False),
                until_date=until,
            )
            log_action(chat_id, "mutes", 0, user_id, time=time_str or "бессрочно", reason=reason)
            await bot.send_message(chat_id, f"🔇 <a href='tg://user?id={user_id}'>Пользователь</a> замучен на {time_str or 'бессрочно'}. Причина: {reason}")
        elif action == "ban":
            await bot.ban_chat_member(chat_id, user_id)
            log_action(chat_id, "bans", 0, user_id, time=time_str or "навсегда", reason=reason)
            await bot.send_message(chat_id, f"🔨 <a href='tg://user?id={user_id}'>Пользователь</a> забанен. Причина: {reason}")
        elif action == "kick":
            await bot.ban_chat_member(chat_id, user_id)
            await bot.unban_chat_member(chat_id, user_id)
            log_action(chat_id, "kicks", 0, user_id)
            await bot.send_message(chat_id, f"👢 <a href='tg://user?id={user_id}'>Пользователь</a> кикнут.")
        elif action == "warn":
            warns = add_warn(user_id, chat_id)
            log_action(chat_id, "warns", 0, user_id, reason=reason)
            await bot.send_message(chat_id, f"⚠️ <a href='tg://user?id={user_id}'>Пользователь</a> получил варн. Всего: {warns}")
    except Exception as e:
        print(f"Punishment error: {e}")


# ==================== БАЗОВЫЕ КОМАНДЫ ====================
@router.message(lambda m: m.text and m.text.lower().startswith(("мут", ".мут", "!мут", "/мут", "mute")))
async def cmd_mute(message: types.Message):
    if not await is_moderator(message, 1):
        await message.reply("❌ Недостаточно прав.")
        return
    target = await get_target(message)
    if not target:
        await message.reply("❌ Ответьте на сообщение или укажите @username.")
        return
    if target.id == OWNER_ID:
        await message.reply("❌ Нельзя замутить владельца.")
        return
    if target.id == message.from_user.id:
        await message.reply("❌ Нельзя замутить себя.")
        return
    duration, time_text, reason = parse_duration_reason(message)
    until = datetime.now() + duration if duration else datetime.now() + timedelta(days=3650)
    try:
        await message.bot.restrict_chat_member(
            chat_id=message.chat.id, user_id=target.id,
            permissions=ChatPermissions(can_send_messages=False),
            until_date=until,
        )
        log_action(message.chat.id, "mutes", message.from_user.id, target.id, time=time_text, reason=reason)
        try:
            log_action(message.chat.id, "mutes", message.from_user.id, target.id, time=time_text, reason=reason)
        except Exception:
            pass
        await message.reply(f"🔇 {target.mention_html()} замучен на {time_text}. Причина: {reason}.")
    except Exception as e:
        await message.reply(f"❌ Ошибка: {e}")


@router.message(lambda m: m.text and m.text.lower().startswith(("размут", ".размут", "унмут", "анмут", "unmute")))
async def cmd_unmute(message: types.Message):
    if not await is_moderator(message, 1):
        return
    target = await get_target(message)
    if not target:
        await message.reply("❌ Ответьте на сообщение.")
        return
    try:
        await message.bot.restrict_chat_member(
            chat_id=message.chat.id, user_id=target.id,
            permissions=FULL_PERMISSIONS,
        )
        await message.reply(f"🔊 {target.mention_html()} размучен.")
    except Exception as e:
        await message.reply(f"❌ Ошибка: {e}")


@router.message(lambda m: m.text and m.text.lower().startswith(("бан", ".бан", "!бан", "/бан", "ban")))
async def cmd_ban(message: types.Message):
    if not await is_moderator(message, 2):
        return
    target = await get_target(message)
    if not target:
        await message.reply("❌ Ответьте на сообщение.")
        return
    if target.id == OWNER_ID:
        await message.reply("❌ Нельзя забанить владельца.")
        return
    time_arg = get_duration_arg(message)
    duration = parse_time(time_arg) if time_arg else None
    try:
        await message.bot.ban_chat_member(message.chat.id, target.id)
        log_action(message.chat.id, "bans", message.from_user.id, target.id,
                   time=time_arg or "навсегда", reason="ручной бан")
        if duration:
            await message.reply(f"🔨 {target.mention_html()} забанен на {time_arg}.")
        else:
            await message.reply(f"🔨 {target.mention_html()} забанен навсегда.")
    except Exception as e:
        await message.reply(f"❌ Ошибка: {e}")


@router.message(lambda m: m.text and m.text.lower().split()[0].lstrip(".!/") in {"разбан", "унбан", "unban"})
async def cmd_unban(message: types.Message):
    if not await is_moderator(message, 2):
        return
    target = await get_target(message)
    target_id = getattr(target, "id", None) if target else None
    target_label = target.mention_html() if target and hasattr(target, "mention_html") else None
    if not target_id:
        args = (message.text or "").split()
        if len(args) > 1 and args[1].isdigit():
            target_id = int(args[1])
            target_label = f"<code>{target_id}</code>"
    if not target_id:
        await message.reply("❌ Ответьте на сообщение пользователя или укажите числовой ID: <code>разбан 123456789</code> / <code>/unban 123456789</code>")
        return
    try:
        await message.bot.unban_chat_member(message.chat.id, int(target_id), only_if_banned=True)
        await message.reply(f"✅ {target_label or f'<code>{target_id}</code>'} разблокирован.")
    except Exception as e:
        await message.reply(f"❌ Ошибка: {e}")


@router.message(lambda m: m.text and m.text.lower().startswith(("кик", ".кик", "!кик", "/кик", "kick")))
async def cmd_kick(message: types.Message):
    if not await is_moderator(message, 1):
        return
    target = await get_target(message)
    if not target:
        await message.reply("❌ Ответьте на сообщение.")
        return
    if target.id == OWNER_ID:
        await message.reply("❌ Нельзя кикнуть владельца.")
        return
    try:
        await message.bot.ban_chat_member(message.chat.id, target.id)
        await message.bot.unban_chat_member(message.chat.id, target.id)
        log_action(message.chat.id, "kicks", message.from_user.id, target.id)
        await message.reply(f"👢 {target.mention_html()} кикнут.")
    except Exception as e:
        await message.reply(f"❌ Ошибка: {e}")


@router.message(lambda m: m.text and m.text.lower().startswith(("варн", ".варн", "пред", "warn")))
async def cmd_warn(message: types.Message):
    if not await is_moderator(message, 1):
        return
    target = await get_target(message)
    if not target:
        await message.reply("❌ Ответьте на сообщение.")
        return
    if target.id == OWNER_ID:
        await message.reply("❌ Нельзя выдать варн владельцу.")
        return
    warns = add_warn(target.id, message.chat.id)
    if warns is None:
        create_user(target.id, message.chat.id, target.username, target.first_name)
        warns = add_warn(target.id, message.chat.id)
    log_action(message.chat.id, "warns", message.from_user.id, target.id, reason="ручной варн")
    await message.reply(f"⚠️ {target.mention_html()} получил варн. Всего: {warns}")
    triggers = chat_triggers[message.chat.id]
    for key in ["warn_3", "warn_5", "warn_7", "warn_10"]:
        t = triggers.get(key)
        if not t or not t.get("enabled"):
            continue
        threshold = int(key.split("_")[1])
        if warns == threshold:
            await apply_punishment(
                message.bot, message.chat.id, target.id,
                t["action"], t["time"], f"лимит варнов ({threshold})",
            )
            reset_warns(target.id, message.chat.id)
            break


@router.message(lambda m: m.text and m.text.lower().startswith(("унварн", "анварн", "снятьварн", "unwarn")))
async def cmd_unwarn(message: types.Message):
    if not await is_moderator(message, 2):
        return
    target = await get_target(message)
    if not target:
        await message.reply("❌ Ответьте на сообщение.")
        return
    reset_warns(target.id, message.chat.id)
    await message.reply(f"✅ Варны {target.mention_html()} сняты.")


@router.message(lambda m: m.text and m.text.lower().startswith(("варны", "преды", "warns")))
async def cmd_warns(message: types.Message):
    target = await get_target(message) or message.from_user
    user = get_user(target.id, message.chat.id)
    if not user:
        await message.reply(f"У {target.mention_html()} нет предупреждений.")
        return
    await message.reply(f"⚠️ {target.mention_html()}: <b>{user['warns']}</b> предупреждений.")


@router.message(lambda m: m.text and m.text.lower().startswith(("повысить", "promote")))
async def cmd_promote(message: types.Message):
    if not await is_moderator(message, 3):
        return
    target = await get_target(message)
    if not target:
        await message.reply("❌ Ответьте на сообщение.")
        return
    current = await get_user_rank(target.id, message.chat.id)
    if current >= 5:
        await message.reply("Максимальный ранг.")
        return
    set_rank(target.id, message.chat.id, current + 1)
    log_action(message.chat.id, "promotes", message.from_user.id, target.id, rank=current + 1)
    await message.reply(f"⬆️ {target.mention_html()} повышен до <b>{RANK_NAMES[current + 1]}</b>")


@router.message(lambda m: m.text and m.text.lower().startswith(("понизить", "demote")))
async def cmd_demote(message: types.Message):
    if not await is_moderator(message, 4):
        return
    target = await get_target(message)
    if not target:
        await message.reply("❌ Ответьте на сообщение.")
        return
    current = await get_user_rank(target.id, message.chat.id)
    if current <= 0:
        return
    set_rank(target.id, message.chat.id, current - 1)
    log_action(message.chat.id, "demotes", message.from_user.id, target.id, rank=current - 1)
    await message.reply(f"⬇️ {target.mention_html()} понижен до <b>{RANK_NAMES[current - 1]}</b>")


@router.message(lambda m: m.text and m.text.lower().startswith(("мойранг", "rank", "ктоя")))
async def cmd_rank(message: types.Message):
    target = message.reply_to_message.from_user if message.reply_to_message else message.from_user
    rank = await get_user_rank(target.id, message.chat.id)
    await message.reply(f"Ранг {target.mention_html()}: <b>{RANK_NAMES.get(rank, 'Участник')}</b>")


# ==================== ЭТАП 5: СООБЩЕНИЯ И ИНФО ====================
@router.message(lambda m: m.text and m.text.lower().startswith(("-смс", "del")))
async def cmd_delete(message: types.Message):
    if not await is_moderator(message, 1):
        return
    if not message.reply_to_message:
        await message.reply("❌ Ответьте на сообщение.")
        return
    try:
        await message.reply_to_message.delete()
        await message.delete()
    except Exception as e:
        await message.reply(f"❌ Ошибка: {e}")


@router.message(lambda m: m.text and m.text.lower().startswith((".пин", "pin")))
async def cmd_pin(message: types.Message):
    if not await is_moderator(message, 2):
        return
    if not message.reply_to_message:
        await message.reply("❌ Ответьте на сообщение.")
        return
    try:
        await message.bot.pin_chat_message(
            chat_id=message.chat.id,
            message_id=message.reply_to_message.message_id,
        )
        await message.reply("📌 Закреплено.")
    except Exception as e:
        await message.reply(f"❌ Ошибка: {e}")


@router.message(lambda m: m.text and m.text.lower().startswith((".анпин", "unpin")))
async def cmd_unpin(message: types.Message):
    if not await is_moderator(message, 2):
        return
    try:
        await message.bot.unpin_chat_message(chat_id=message.chat.id)
        await message.reply("📌 Откреплено.")
    except Exception as e:
        await message.reply(f"❌ Ошибка: {e}")


@router.message(lambda m: m.text and m.text.lower().startswith(("очистить", "clear")))
async def cmd_clear(message: types.Message):
    if not await is_moderator(message, 2):
        return
    args = message.text.split()
    if len(args) < 2 or not args[1].isdigit():
        await message.reply("❌ Формат: <code>очистить 10</code>")
        return
    count = min(int(args[1]), 100)
    deleted = 0
    for offset in range(1, count + 1):
        try:
            await message.bot.delete_message(chat_id=message.chat.id, message_id=message.message_id - offset)
            deleted += 1
        except Exception:
            pass
    await message.answer(f"🧹 Удалено: {deleted}.")


@router.message(lambda m: m.text and m.text.lower().startswith(("кто админ", "admins")))
async def cmd_admins(message: types.Message):
    try:
        admins = await message.chat.get_administrators()
        text = "<b>👑 Администраторы:</b>\n\n"
        for admin in admins:
            if admin.user.is_bot:
                continue
            status = "Создатель" if admin.status == "creator" else "Админ"
            text += f"• {admin.user.mention_html()} — {status}\n"
        await message.reply(text)
    except Exception as e:
        await message.reply(f"❌ Ошибка: {e}")


@router.message(lambda m: m.text and m.text.lower().startswith(("кто модер", "модеры")))
async def cmd_moders(message: types.Message):
    try:
        admins = await message.chat.get_administrators()
        text = "<b>🛡️ Модерация:</b>\n\n"
        for admin in admins:
            if admin.user.is_bot:
                continue
            rank = await get_user_rank(admin.user.id, message.chat.id)
            text += f"• {admin.user.mention_html()} — {RANK_NAMES.get(rank, 'Участник')}\n"
        await message.reply(text)
    except Exception as e:
        await message.reply(f"❌ Ошибка: {e}")


@router.message(lambda m: m.text and m.text.lower().startswith(("инфо", "info")))
async def cmd_info(message: types.Message):
    target = await get_target(message) or message.from_user
    user = get_user(target.id, message.chat.id)
    if not user:
        create_user(target.id, message.chat.id, target.username, target.first_name)
        user = get_user(target.id, message.chat.id)
    rank = await get_user_rank(target.id, message.chat.id)
    text = (
        f"<b>👤 Информация</b>\n\n"
        f"• Имя: {target.full_name}\n"
        f"• Username: @{target.username or 'нет'}\n"
        f"• ID: <code>{target.id}</code>\n"
        f"• Ранг: <b>{RANK_NAMES.get(rank, 'Участник')}</b>\n"
        f"• Варнов: <b>{user['warns']}</b>\n"
    )
    await message.reply(text)


@router.message(lambda m: m.text and m.text.lower().startswith("топ"))
async def cmd_top(message: types.Message):
    stats = chat_stats[message.chat.id]
    if not stats["users"]:
        await message.reply("Пока нет статистики.")
        return
    sorted_users = sorted(stats["users"].items(), key=lambda x: x[1], reverse=True)[:10]
    text = "<b>🏆 Топ активных:</b>\n\n"
    for i, (uid, count) in enumerate(sorted_users, 1):
        try:
            member = await message.chat.get_member(uid)
            text += f"{i}. {member.user.mention_html()} — {count}\n"
        except Exception:
            text += f"{i}. ID {uid} — {count}\n"
    await message.reply(text)


# ==================== ЭТАП 6: РАНГИ ====================
@router.message(lambda m: m.text and re.match(r"^[!]{1,5}модер(\s|$)", m.text.lower().strip()))
async def cmd_quick_moder(message: types.Message):
    if not await is_moderator(message, 4):
        return
    exclamations = 0
    for char in message.text:
        if char == "!":
            exclamations += 1
        else:
            break
    rank = min(exclamations, 5)
    target = await get_target(message)
    if not target:
        await message.reply("❌ Ответьте на сообщение.")
        return
    if target.id == OWNER_ID:
        await message.reply("❌ Нельзя менять ранг владельца.")
        return
    set_rank(target.id, message.chat.id, rank)
    log_action(message.chat.id, "promotes", message.from_user.id, target.id, rank=rank)
    await message.reply(f"✅ {target.mention_html()} → <b>{RANK_NAMES[rank]}</b>")


@router.message(lambda m: m.text and m.text.lower().startswith(("снять модера", "разжаловать")))
async def cmd_remove_moder(message: types.Message):
    if not await is_moderator(message, 4):
        return
    target = await get_target(message)
    if not target:
        await message.reply("❌ Ответьте на сообщение.")
        return
    if target.id == OWNER_ID:
        await message.reply("❌ Нельзя снять права владельца.")
        return
    set_rank(target.id, message.chat.id, 0)
    log_action(message.chat.id, "demotes", message.from_user.id, target.id, rank=0)
    await message.reply(f"✅ {target.mention_html()} лишён прав.")


@router.message(lambda m: m.text and m.text.lower().startswith(("права", "perms")))
async def cmd_perms(message: types.Message):
    target = await get_target(message) or message.from_user
    rank = await get_user_rank(target.id, message.chat.id)
    text = (
        f"<b>🛡️ Права {target.full_name}</b>\n\n"
        f"• Ранг: <b>{RANK_NAMES.get(rank, 'Участник')}</b>\n"
        f"• Мут: {'✅' if rank >= 1 else '❌'}\n"
        f"• Бан: {'✅' if rank >= 2 else '❌'}\n"
        f"• Повышение: {'✅' if rank >= 3 else '❌'}\n"
        f"• Понижение: {'✅' if rank >= 4 else '❌'}\n"
    )
    await message.reply(text)


# ==================== ЭТАП 7: ЛОГИ ====================
@router.message(lambda m: m.text and m.text.lower().startswith("логи"))
async def cmd_logs(message: types.Message):
    if not await is_moderator(message, 3):
        return
    logs = chat_logs[message.chat.id]
    text = "<b>📋 Последние логи:</b>\n\n"
    for key, name in [("mutes", "🔇 Муты"), ("bans", "🔨 Баны"), ("warns", "⚠️ Варны"), ("promotes", "⬆️ Повышения")]:
        if logs[key]:
            text += f"<b>{name}:</b>\n"
            for entry in logs[key][-5:]:
                text += f"• <a href='tg://user?id={entry['target']}'>Цель</a>\n"
            text += "\n"
    if text == "<b>📋 Последние логи:</b>\n\n":
        text += "Пока нет записей."
    await message.reply(text)


@router.message(lambda m: m.text and m.text.lower().startswith("топ нарушителей"))
async def cmd_top_violators(message: types.Message):
    v = violators[message.chat.id]
    if not v:
        await message.reply("Пока нет нарушителей.")
        return
    sorted_v = sorted(v.items(), key=lambda x: x[1], reverse=True)[:10]
    text = "<b>🚨 Топ нарушителей:</b>\n\n"
    for i, (uid, count) in enumerate(sorted_v, 1):
        try:
            member = await message.chat.get_member(uid)
            text += f"{i}. {member.user.mention_html()} — {count}\n"
        except Exception:
            text += f"{i}. ID {uid} — {count}\n"
    await message.reply(text)


@router.message(lambda m: m.text and m.text.lower().startswith(("отчёт", "отчет")))
async def cmd_report(message: types.Message):
    if not await is_moderator(message, 3):
        return
    logs = chat_logs[message.chat.id]
    stats = chat_stats[message.chat.id]
    text = (
        f"<b>📊 Отчёт по чату</b>\n\n"
        f"• Сообщений: <b>{stats['messages']}</b>\n"
        f"• Участников: <b>{len(stats['users'])}</b>\n\n"
        f"<b>Модерация:</b>\n"
        f"• Мутов: <b>{len(logs['mutes'])}</b>\n"
        f"• Банов: <b>{len(logs['bans'])}</b>\n"
        f"• Варнов: <b>{len(logs['warns'])}</b>\n"
        f"• Киков: <b>{len(logs['kicks'])}</b>\n"
    )
    await message.reply(text)


# ==================== ЭТАП 8: ГЛОБАЛЬНАЯ МОДЕРАЦИЯ ====================
@router.message(lambda m: m.text and m.text.lower().startswith(("глобан", ".глобан", "!глобан")))
async def cmd_global_ban(message: types.Message):
    """Глобальный бан — бан во всех чатах сетки."""
    if message.from_user.id != OWNER_ID:
        await message.reply("❌ Только владелец может использовать глобальную модерацию.")
        return

    target = await get_target(message)
    if not target:
        await message.reply("❌ Ответьте на сообщение или укажите @username.")
        return
    if target.id == OWNER_ID:
        await message.reply("❌ Нельзя забанить владельца.")
        return

    # Добавляем в глобальный бан-лист
    add_global_ban(target.id, reason="глобан")

    # Баним во всех чатах сетки
    chats = get_global_chats()
    banned_count = 0

    # Если чатов нет — добавляем текущий
    if not chats:
        add_global_chat(message.chat.id, message.chat.title)
        chats = get_global_chats()

    for chat in chats:
        try:
            await message.bot.ban_chat_member(chat["chat_id"], target.id)
            banned_count += 1
        except Exception:
            pass

    await message.reply(
        f"🌐 {target.mention_html()} глобально забанен в <b>{banned_count}</b> чатах."
    )


@router.message(lambda m: m.text and m.text.lower().startswith(("глоразбан", ".глоразбан")))
async def cmd_global_unban(message: types.Message):
    """Глобальный разбан."""
    if message.from_user.id != OWNER_ID:
        await message.reply("❌ Только владелец.")
        return

    target = await get_target(message)
    if not target:
        await message.reply("❌ Ответьте на сообщение.")
        return

    remove_global_ban(target.id)

    chats = get_global_chats()
    unbanned_count = 0
    for chat in chats:
        try:
            await message.bot.unban_chat_member(chat["chat_id"], target.id)
            unbanned_count += 1
        except Exception:
            pass

    await message.reply(f"✅ {target.mention_html()} глобально разбанен в <b>{unbanned_count}</b> чатах.")


@router.message(lambda m: m.text and m.text.lower().startswith(("гломут", ".гломут")))
async def cmd_global_mute(message: types.Message):
    """Глобальный мут."""
    if message.from_user.id != OWNER_ID:
        await message.reply("❌ Только владелец.")
        return

    target = await get_target(message)
    if not target:
        await message.reply("❌ Ответьте на сообщение.")
        return

    chats = get_global_chats()
    if not chats:
        add_global_chat(message.chat.id, message.chat.title)
        chats = get_global_chats()

    muted_count = 0
    for chat in chats:
        try:
            await message.bot.restrict_chat_member(
                chat_id=chat["chat_id"], user_id=target.id,
                permissions=ChatPermissions(can_send_messages=False),
                until_date=datetime.now() + timedelta(days=3650),
            )
            muted_count += 1
        except Exception:
            pass

    await message.reply(f"🌐 {target.mention_html()} глобально замучен в <b>{muted_count}</b> чатах.")


@router.message(lambda m: m.text and m.text.lower().startswith(("добавить чат", ".добавить чат", "+сетка")))
async def cmd_add_chat(message: types.Message):
    """Добавить чат в сетку."""
    if message.from_user.id != OWNER_ID:
        await message.reply("❌ Только владелец.")
        return
    add_global_chat(message.chat.id, message.chat.title)
    await message.reply(f"✅ Чат <b>{message.chat.title}</b> добавлен в сетку.")


@router.message(lambda m: m.text and m.text.lower().startswith(("убрать чат", "-сетка")))
async def cmd_remove_chat(message: types.Message):
    """Убрать чат из сетки."""
    if message.from_user.id != OWNER_ID:
        return
    remove_global_chat(message.chat.id)
    await message.reply(f"✅ Чат убран из сетки.")


@router.message(lambda m: m.text and m.text.lower().startswith(("сетка", ".сетка")))
async def cmd_list_chats(message: types.Message):
    """Показать все чаты сетки."""
    if message.from_user.id != OWNER_ID:
        return
    chats = get_global_chats()
    if not chats:
        await message.reply("Сетка пуста.")
        return
    text = "<b>🌐 Чаты сетки:</b>\n\n"
    for chat in chats:
        text += f"• {chat.get('title', 'Без названия')} (<code>{chat['chat_id']}</code>)\n"
    await message.reply(text)


# ==================== УПРАВЛЕНИЕ ФИЛЬТРАМИ ====================
@router.message(lambda m: m.text and m.text.lower().startswith("+антиссылка"))
async def cmd_antilink_on(message: types.Message):
    if not await is_moderator(message, 2):
        return
    chat_filters[message.chat.id]["antilink"] = True
    await message.reply("✅ Антиссылки включены.")


@router.message(lambda m: m.text and m.text.lower().startswith("-антиссылка"))
async def cmd_antilink_off(message: types.Message):
    if not await is_moderator(message, 2):
        return
    chat_filters[message.chat.id]["antilink"] = False
    await message.reply("❌ Антиссылки выключены.")


@router.message(lambda m: m.text and m.text.lower().startswith("+антимат"))
async def cmd_antimat_on(message: types.Message):
    if not await is_moderator(message, 2):
        return
    chat_filters[message.chat.id]["antimat"] = True
    await message.reply("✅ Антимат включён.")


@router.message(lambda m: m.text and m.text.lower().startswith("-антимат"))
async def cmd_antimat_off(message: types.Message):
    if not await is_moderator(message, 2):
        return
    chat_filters[message.chat.id]["antimat"] = False
    await message.reply("❌ Антимат выключен.")


@router.message(lambda m: m.text and m.text.lower().startswith("+антифлуд"))
async def cmd_antiflood_on(message: types.Message):
    if not await is_moderator(message, 2):
        return
    chat_filters[message.chat.id]["antiflood"] = True
    await message.reply("✅ Антифлуд включён.")


@router.message(lambda m: m.text and m.text.lower().startswith("-антифлуд"))
async def cmd_antiflood_off(message: types.Message):
    if not await is_moderator(message, 2):
        return
    chat_filters[message.chat.id]["antiflood"] = False
    await message.reply("❌ Антифлуд выключен.")


# ==================== ТРИГГЕРЫ ====================
@router.message(lambda m: m.text and m.text.lower().startswith("триггер варн"))
async def cmd_set_warn_trigger(message: types.Message):
    if not await is_moderator(message, 3):
        return
    args = message.text.split()
    if len(args) < 5:
        await message.reply("Формат: <code>триггер варн 3 мут 1ч</code>")
        return
    try:
        threshold = int(args[2])
    except ValueError:
        await message.reply("❌ Число варнов должно быть числом.")
        return
    action_map = {"мут": "mute", "бан": "ban", "кик": "kick", "варн": "warn"}
    action = action_map.get(args[3].lower())
    if not action:
        await message.reply("❌ Действие: мут, бан, кик, варн")
        return
    time_str = args[4] if len(args) > 4 else "0"
    chat_triggers[message.chat.id][f"warn_{threshold}"] = {
        "action": action, "time": time_str, "enabled": True,
    }
    await message.reply(f"✅ Триггер: <b>{threshold}</b> варнов → <b>{args[3]}</b> на <b>{time_str}</b>")


@router.message(lambda m: m.text and m.text.lower().startswith("триггеры"))
async def cmd_list_triggers(message: types.Message):
    triggers = chat_triggers[message.chat.id]
    text = "<b>📋 Триггеры:</b>\n\n"
    for key in ["warn_3", "warn_5", "warn_7", "warn_10"]:
        t = triggers.get(key)
        if t and t.get("enabled"):
            threshold = key.split("_")[1]
            text += f"• {threshold} варнов → {t['action']} на {t['time']}\n"
    await message.reply(text)


# ==================== АВТОФИЛЬТРЫ ====================
@router.message()
async def auto_filters(message: types.Message):
    if message.from_user.id == OWNER_ID:
        return

    chat_id = message.chat.id
    chat_stats[chat_id]["messages"] += 1
    chat_stats[chat_id]["users"][message.from_user.id] += 1

    # Проверка глобального бана
    if is_globally_banned(message.from_user.id):
        try:
            await message.bot.ban_chat_member(chat_id, message.from_user.id)
        except Exception:
            pass
        return

    if await has_rank(message, 1):
        return

    filters = chat_filters[chat_id]
    triggers = chat_triggers[chat_id]

    if filters["antibot"] and message.from_user.is_bot:
        try:
            await message.bot.ban_chat_member(chat_id, message.from_user.id)
            await message.answer(f"🤖 Бот забанен.")
        except Exception:
            pass
        return

    if filters["antisticker"] and message.sticker:
        await apply_filter_punishment(message, "стикер")
        return
    if filters["antigif"] and message.animation:
        await apply_filter_punishment(message, "GIF")
        return
    if filters["antiphoto"] and message.photo:
        await apply_filter_punishment(message, "фото")
        return
    if filters["antivideo"] and message.video:
        await apply_filter_punishment(message, "видео")
        return
    if filters["antivoice"] and message.voice:
        await apply_filter_punishment(message, "голосовое")
        return
    if filters["antidocument"] and message.document:
        await apply_filter_punishment(message, "документ")
        return

    if not message.text:
        return

    text = message.text
    lower_text = text.lower()

    if triggers["link_enabled"] and URL_PATTERN.search(text):
        await apply_punishment(message.bot, chat_id, message.from_user.id,
                               triggers["link_action"], triggers["link_time"], "ссылка в чате")
        return

    if triggers["mat_enabled"]:
        for word in BAD_WORDS:
            if word in lower_text:
                await apply_punishment(message.bot, chat_id, message.from_user.id,
                                       triggers["mat_action"], triggers["mat_time"], "нецензурная лексика")
                return

    if filters["antiall"] and ALL_PATTERN.search(text):
        await apply_filter_punishment(message, "@all")
        return

    if filters["antilink"] and URL_PATTERN.search(text):
        await apply_filter_punishment(message, "ссылка")
        return

    if filters["antiinvite"] and ("t.me/joinchat" in lower_text or "telegram.me/joinchat" in lower_text):
        await apply_filter_punishment(message, "инвайт")
        return

    if filters["antimat"]:
        for word in BAD_WORDS:
            if word in lower_text:
                await apply_filter_punishment(message, "мат")
                return

    if filters["antireklama"]:
        for word in REKLAMA_WORDS:
            if word in lower_text:
                await apply_filter_punishment(message, "реклама")
                return

    if filters["antikripta"]:
        for word in KRIPTA_WORDS:
            if word in lower_text:
                await apply_filter_punishment(message, "крипта")
                return

    if filters["antifishing"]:
        for pattern in FISHING_PATTERNS:
            if re.search(pattern, lower_text):
                await apply_filter_punishment(message, "фишинг")
                return

    if filters["anticaps"] and len(text) > 5:
        letters = [c for c in text if c.isalpha()]
        if letters:
            caps = sum(1 for c in letters if c.isupper())
            percent = (caps / len(letters)) * 100
            if percent >= filters["anticaps_percent"]:
                await apply_filter_punishment(message, "капс")
                return

    if filters["antiflood"]:
        now = datetime.now()
        window = filters["antiflood_window"]
        limit = filters["antiflood_limit"]
        flood_tracker[chat_id][message.from_user.id] = [
            t for t in flood_tracker[chat_id][message.from_user.id]
            if (now - t).total_seconds() < window
        ]
        flood_tracker[chat_id][message.from_user.id].append(now)
        if len(flood_tracker[chat_id][message.from_user.id]) > limit:
            try:
                await message.bot.restrict_chat_member(
                    chat_id=chat_id, user_id=message.from_user.id,
                    permissions=ChatPermissions(can_send_messages=False),
                    until_date=now + timedelta(minutes=5),
                )
                log_action(chat_id, "mutes", 0, message.from_user.id, time="5м", reason="флуд")
                await message.answer(f"🌊 {message.from_user.mention_html()} замучен на 5 минут за флуд.")
                flood_tracker[chat_id][message.from_user.id] = []
            except Exception:
                pass


# ── Compatibility note for the standalone legacy module ──────────────────────
# This file is a standalone Router and depends on database.crud and
# middlewares.check_rank. The main production entrypoint in this repository is
# iris.py, which uses its own single Router and Supabase helpers. Do not import
# this file into iris.py without reconciling duplicate handlers and dependencies.
# Both English and Russian unban aliases exist above; the recommended current
# implementation supporting reply + numeric ID is in iris.py.
