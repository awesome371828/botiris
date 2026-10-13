"""
Модуль модерации с полным набором команд.
Поддерживает русские и английские команды, все сроки, систему варнов,
автоматические наказания и проверку прав.
"""

import re
from datetime import datetime, timedelta
from aiogram import Router, types, F
from aiogram.types import ChatPermissions
from database.crud import (
    get_user,
    create_user,
    add_warn,
    reset_warns,
    get_chat,
    set_rank,
)
from middlewares.check_rank import has_rank, get_user_rank, OWNER_ID

router = Router()

# ==================== КОНСТАНТЫ ====================
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
def parse_time(text: str) -> timedelta | None:
    """
    Парсит строку времени в timedelta.
    Поддерживает: секунды, минуты, часы, дни, недели, месяцы, годы.
    Примеры: 10с, 5м, 1 час, 43 минуты, 2 недели, 1 месяц, 1 год.
    """
    if not text:
        return None

    text = text.lower().strip()

    time_units = {
        # Секунды
        "сек": 1, "секунда": 1, "секунды": 1, "секунд": 1, "с": 1,
        # Минуты
        "мин": 60, "минута": 60, "минуты": 60, "минут": 60, "м": 60,
        # Часы
        "час": 3600, "часа": 3600, "часов": 3600, "ч": 3600,
        # Дни
        "день": 86400, "дня": 86400, "дней": 86400, "д": 86400, "сутки": 86400,
        # Недели
        "нед": 604800, "неделя": 604800, "недели": 604800, "недель": 604800, "н": 604800,
        # Месяцы (30 дней)
        "мес": 2592000, "месяц": 2592000, "месяца": 2592000, "месяцев": 2592000,
        # Годы (365 дней)
        "год": 31536000, "года": 31536000, "лет": 31536000, "г": 31536000,
    }

    # Сначала пробуем формат "1 час"
    match = re.match(r"^(\d+)\s*([а-яa-z]+)$", text)
    if match:
        num = int(match.group(1))
        unit = match.group(2)
        for key, multiplier in time_units.items():
            if unit.startswith(key) or key.startswith(unit):
                return timedelta(seconds=num * multiplier)
        return None

    # Пробуем найти число в начале строки
    match = re.match(r"^(\d+)", text)
    if not match:
        return None

    num = int(match.group(1))
    rest = text[match.end():].strip()

    for unit, multiplier in time_units.items():
        if unit in rest:
            return timedelta(seconds=num * multiplier)

    return None


# ==================== ПРОВЕРКА ПРАВ ====================
async def is_moderator(message: types.Message, required: int = 1) -> bool:
    if message.from_user.id == OWNER_ID:
        return True
    return await has_rank(message, required)


# ==================== ПОЛУЧЕНИЕ ЦЕЛИ ====================
async def get_target(message: types.Message) -> types.User | None:
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


# ==================== ИЗВЛЕЧЕНИЕ СРОКА ====================
def get_duration_arg(message: types.Message) -> str | None:
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
@router.message(
    lambda m: m.text
    and m.text.lower().startswith(
        ("мут", ".мут", "!мут", "/мут", "mute", ".mute", "!mute", "/mute")
    )
)
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

    time_arg = get_duration_arg(message)
    duration = parse_time(time_arg) if time_arg else None

    if duration:
        until = datetime.now() + duration
        time_text = time_arg
    else:
        until = datetime.now() + timedelta(days=3650)
        time_text = "бессрочно"

    try:
        await message.bot.restrict_chat_member(
            chat_id=message.chat.id,
            user_id=target.id,
            permissions=ChatPermissions(can_send_messages=False),
            until_date=until,
        )
        await message.reply(f"🔇 {target.mention_html()} замучен на {time_text}.")
    except Exception as e:
        await message.reply(f"❌ Ошибка: {e}")


# ==================== РАЗМУТ ====================
@router.message(
    lambda m: m.text
    and m.text.lower().startswith(
        (
            "размут", ".размут", "!размут", "/размут",
            "унмут", ".унмут", "!унмут", "/унмут",
            "unmute", ".unmute", "!unmute", "/unmute",
        )
    )
)
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
@router.message(
    lambda m: m.text
    and m.text.lower().startswith(
        ("бан", ".бан", "!бан", "/бан", "ban", ".ban", "!ban", "/ban")
    )
)
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
            await message.reply(
                f"🔨 {target.mention_html()} забанен на {time_arg}."
            )
        else:
            await message.reply(f"🔨 {target.mention_html()} забанен навсегда.")
    except Exception as e:
        await message.reply(f"❌ Ошибка: {e}")


# ==================== РАЗБАН ====================
@router.message(
    lambda m: m.text
    and m.text.lower().startswith(
        (
            "разбан", ".разбан", "!разбан", "/разбан",
            "унбан", ".унбан", "!унбан", "/унбан",
            "unban", ".unban", "!unban", "/unban",
        )
    )
)
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
@router.message(
    lambda m: m.text
    and m.text.lower().startswith(
        ("кик", ".кик", "!кик", "/кик", "kick", ".kick", "!kick", "/kick")
    )
)
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
@router.message(
    lambda m: m.text
    and m.text.lower().startswith(
        (
            "варн", ".варн", "!варн", "/варн",
            "пред", ".пред", "!пред", "/пред",
            "warn", ".warn", "!warn", "/warn",
        )
    )
)
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


# ==================== СНЯТЬ ВАРН ====================
@router.message(
    lambda m: m.text
    and m.text.lower().startswith(
        (
            "унварн", ".унварн", "!унварн", "/унварн",
            "снятьварн", ".снятьварн", "!снятьварн", "/снятьварн",
            "анварн", ".анварн", "!анварн", "/анварн",
            "unwarn", ".unwarn", "!unwarn", "/unwarn",
        )
    )
)
async def cmd_unwarn(message: types.Message):
    if not await is_moderator(message, 2):
        await message.reply("❌ Недостаточно прав.")
        return

    target = await get_target(message)
    if not target:
        await message.reply("❌ Ответьте на сообщение или укажите @username.")
        return

    reset_warns(target.id, message.chat.id)
    await message.reply(f"✅ Варны {target.mention_html()} сброшены.")


# ==================== ПРОВЕРКА ВАРНОВ ====================
@router.message(
    lambda m: m.text
    and m.text.lower().startswith(
        (
            "варны", ".варны", "!варны", "/варны",
            "преды", ".преды", "!преды", "/преды",
            "warns", ".warns", "!warns", "/warns",
        )
    )
)
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
@router.message(
    lambda m: m.text
    and m.text.lower().startswith(
        (
            "повысить", ".повысить", "!повысить", "/повысить",
            "promote", ".promote", "!promote", "/promote",
        )
    )
)
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
@router.message(
    lambda m: m.text
    and m.text.lower().startswith(
        (
            "понизить", ".понизить", "!понизить", "/понизить",
            "demote", ".demote", "!demote", "/demote",
        )
    )
)
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
@router.message(
    lambda m: m.text
    and m.text.lower().startswith(
        (
            "мойранг", ".мойранг", "!мойранг", "/мойранг",
            "rank", ".rank", "!rank", "/rank",
            "ктоя", ".ктоя", "!ктоя", "/ктоя",
        )
    )
)
async def cmd_rank(message: types.Message):
    target = message.reply_to_message.from_user if message.reply_to_message else message.from_user
    rank = await get_user_rank(target.id, message.chat.id)
    await message.reply(f"Ранг {target.mention_html()}: <b>{RANK_NAMES.get(rank, 'Участник')}</b>")
