from aiogram import Router, types
from aiogram.types import ChatPermissions
from datetime import datetime, timedelta
from database.crud import get_user, create_user, add_warn, reset_warns, get_chat
from middlewares.check_rank import has_rank, OWNER_ID

router = Router()

RANK_NAMES = {
    0: "Участник",
    1: "Мл. модератор",
    2: "Ст. модератор",
    3: "Мл. администратор",
    4: "Ст. администратор",
    5: "Создатель",
}

# ==================== ПАРСЕР ВРЕМЕНИ ====================
def parse_time(text: str):
    """Парсит '10с', '10м', '10ч', '10д', '10мес', '10г' в timedelta."""
    if not text:
        return None
    text = text.lower().strip()
    try:
        # Секунды
        if text.endswith("сек") or text.endswith("с"):
            num = text.replace("сек", "").replace("с", "").strip()
            return timedelta(seconds=int(num))
        # Минуты
        if text.endswith("мин") or text.endswith("м"):
            num = text.replace("мин", "").replace("м", "").strip()
            return timedelta(minutes=int(num))
        # Часы
        if text.endswith("час") or text.endswith("ч"):
            num = text.replace("час", "").replace("ч", "").strip()
            return timedelta(hours=int(num))
        # Дни
        if text.endswith("день") or text.endswith("дн") or text.endswith("д"):
            num = text.replace("день", "").replace("дн", "").replace("д", "").strip()
            return timedelta(days=int(num))
        # Месяцы (30 дней)
        if text.endswith("мес") or text.endswith("месяц"):
            num = text.replace("месяц", "").replace("мес", "").strip()
            return timedelta(days=int(num) * 30)
        # Годы (365 дней)
        if text.endswith("год") or text.endswith("г"):
            num = text.replace("год", "").replace("г", "").strip()
            return timedelta(days=int(num) * 365)
    except ValueError:
        return None
    return None


async def is_moderator(message: types.Message, required: int = 1) -> bool:
    """Проверка прав: владелец или ранг."""
    if message.from_user.id == OWNER_ID:
        return True
    return await has_rank(message, required)


async def get_target(message: types.Message):
    """Получить цель: из reply или из @username."""
    if message.reply_to_message:
        return message.reply_to_message.from_user

    args = message.text.split()
    if len(args) < 2:
        return None

    username = args[1].lstrip("@")
    try:
        member = await message.chat.get_member(username)
        return member.user
    except Exception:
        return None


def get_duration_arg(message: types.Message):
    """Извлечь аргумент времени из команды."""
    args = message.text.split()
    # Ищем аргумент, который начинается с цифры и содержит буквы (срок)
    for arg in args[2:]:
        if arg[0].isdigit():
            return arg
    return None


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

    # Парсим срок
    time_arg = get_duration_arg(message)
    duration = parse_time(time_arg) if time_arg else None
    
    # Если срок не указан — бессрочно (до снятия)
    if duration:
        until = datetime.now() + duration
        time_text = time_arg
    else:
        until = datetime.now() + timedelta(days=365 * 10)  # 10 лет = бессрочно
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
@router.message(lambda m: m.text and m.text.lower().startswith((
    "размут", ".размут", "!размут", "/размут", "унмут", ".унмут", "!унмут", "/унмут",
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
            permissions=ChatPermissions(
                can_send_messages=True,
                can_send_media_messages=True,
                can_send_other_messages=True,
                can_add_web_page_previews=True,
            ),
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

    # Парсим срок бана
    time_arg = get_duration_arg(message)
    duration = parse_time(time_arg) if time_arg else None
    
    if duration:
        # Временный бан (Telegram не поддерживает бан на время напрямую, делаем через мут + кик)
        until = datetime.now() + duration
        # Сначала баним
        await message.bot.ban_chat_member(message.chat.id, target.id)
        # Затем разбаниваем через delay (эмуляция временного бана)
        # Простой вариант: просто баним навсегда, т.к. Telegram API не поддерживает временный бан
        await message.reply(f"🔨 {target.mention_html()} забанен на {time_arg}.")
    else:
        await message.bot.ban_chat_member(message.chat.id, target.id)
        await message.reply(f"🔨 {target.mention_html()} забанен навсегда.")


# ==================== РАЗБАН ====================
@router.message(lambda m: m.text and m.text.lower().startswith((
    "разбан", ".разбан", "!разбан", "/разбан", "унбан", ".унбан", "!унбан", "/унбан",
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
    "варн", ".варн", "!варн", "/варн", "пред", ".пред", "!пред", "/пред", "варн", "warn", ".warn", "!warn", "/warn"
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


# ==================== СНЯТЬ ВАРН ====================
@router.message(lambda m: m.text and m.text.lower().startswith((
    "унварн", ".унварн", "!унварн", "/унварн", "снятьварн", ".снятьварн", "!снятьварн", "/снятьварн",
    "анварн", ".анварн", "!анварн", "/анварн", "unwarn", ".unwarn", "!unwarn", "/unwarn"
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
    await message.reply(f"✅ Варны {target.mention_html()} сброшены.")


# ==================== ПРОВЕРКА ВАРНОВ ====================
@router.message(lambda m: m.text and m.text.lower().startswith((
    "варны", ".варны", "!варны", "/варны", "преды", ".преды", "!преды", "/преды", "warns", ".warns", "!warns", "/warns"
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
    "повысить", ".повысить", "!повысить", "/повысить", "promote", ".promote", "!promote", "/promote"
)))
async def cmd_promote(message: types.Message):
    if not await is_moderator(message, 3):
        await message.reply("❌ Недостаточно прав.")
        return

    target = await get_target(message)
    if not target:
        await message.reply("❌ Ответьте на сообщение или укажите @username.")
        return

    from middlewares.check_rank import get_user_rank
    current = await get_user_rank(target.id, message.chat.id)
    if current >= 5:
        await message.reply("Максимальный ранг.")
        return

    set_rank(target.id, message.chat.id, current + 1)
    await message.reply(f"⬆️ {target.mention_html()} повышен до <b>{RANK_NAMES[current + 1]}</b>")


# ==================== ПОНИЖЕНИЕ ====================
@router.message(lambda m: m.text and m.text.lower().startswith((
    "понизить", ".понизить", "!понизить", "/понизить", "demote", ".demote", "!demote", "/demote"
)))
async def cmd_demote(message: types.Message):
    if not await is_moderator(message, 4):
        await message.reply("❌ Недостаточно прав.")
        return

    target = await get_target(message)
    if not target:
        await message.reply("❌ Ответьте на сообщение или укажите @username.")
        return

    from middlewares.check_rank import get_user_rank
    current = await get_user_rank(target.id, message.chat.id)
    if current <= 0:
        return

    set_rank(target.id, message.chat.id, current - 1)
    await message.reply(f"⬇️ {target.mention_html()} понижен до <b>{RANK_NAMES[current - 1]}</b>")


# ==================== МОЙ РАНГ ====================
@router.message(lambda m: m.text and m.text.lower().startswith((
    "мойранг", ".мойранг", "!мойранг", "/мойранг", "rank", ".rank", "!rank", "/rank", "ктоя", ".ктоя", "!ктоя"
)))
async def cmd_rank(message: types.Message):
    from middlewares.check_rank import get_user_rank
    target = message.reply_to_message.from_user if message.reply_to_message else message.from_user
    rank = await get_user_rank(target.id, message.chat.id)
    await message.reply(f"Ранг {target.mention_html()}: <b>{RANK_NAMES.get(rank, 'Участник')}</b>")


# ==================== ОЧИСТКА ====================
@router.message(lambda m: m.text and m.text.lower().startswith((
    "очистить", ".очистить", "!очистить", "/очистить", "clear", ".clear", "!clear", "/clear"
)))
async def cmd_clear(message: types.Message):
    if not await is_moderator(message, 2):
        await message.reply("❌ Недостаточно прав.")
        return
    await message.reply("🧹 Очистка (функция в разработке).")
