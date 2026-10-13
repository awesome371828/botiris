from aiogram import Router, types
from aiogram.filters import Command
from aiogram.types import ChatPermissions
from datetime import datetime, timedelta
from database.crud import get_user, create_user, add_warn, reset_warns, get_chat
from middlewares.check_rank import has_rank

router = Router()

# ID владельца — у него всегда максимальные права
OWNER_ID = 6652898792


def parse_time(text: str):
    """Парсит '10м', '1ч', '2д', '30с' в timedelta."""
    if not text:
        return None
    text = text.lower().strip()
    try:
        if text.endswith("с"):
            return timedelta(seconds=int(text[:-1]))
        if text.endswith("м"):
            return timedelta(minutes=int(text[:-1]))
        if text.endswith("ч"):
            return timedelta(hours=int(text[:-1]))
        if text.endswith("д"):
            return timedelta(days=int(text[:-1]))
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
        # Пытаемся получить участника по username через API чата
        member = await message.chat.get_member(username)
        return member.user
    except Exception:
        return None


# ---------- МУТ ----------
@router.message(lambda m: m.text and m.text.lower().startswith(("мут", ".мут", "!мут")))
async def cmd_mute(message: types.Message):
    if not await is_moderator(message, 1):
        await message.reply("❌ Недостаточно прав.")
        return

    target = await get_target(message)
    if not target:
        await message.reply("❌ Ответьте на сообщение или укажите @username.")
        return
    if target.id == message.from_user.id:
        await message.reply("❌ Нельзя замутить самого себя.")
        return
    if target.id == OWNER_ID:
        await message.reply("❌ Нельзя замутить владельца.")
        return

    # Парсим срок: если после юзернейма есть "10м" и т.д.
    args = message.text.split()
    duration = None
    if len(args) >= 3:
        duration = parse_time(args[2])

    until = datetime.now() + duration if duration else datetime.now() + timedelta(days=365)

    try:
        await message.bot.restrict_chat_member(
            chat_id=message.chat.id,
            user_id=target.id,
            permissions=ChatPermissions(can_send_messages=False),
            until_date=until,
        )
        if duration:
            await message.reply(f"🔇 {target.mention_html()} замучен на {args[2]}.")
        else:
            await message.reply(f"🔇 {target.mention_html()} замучен бессрочно.")
    except Exception as e:
        await message.reply(f"❌ Ошибка: {e}")


# ---------- РАЗМУТ ----------
@router.message(lambda m: m.text and m.text.lower().startswith(("размут", ".размут", "!размут", "унмут", ".унмут")))
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


# ---------- БАН ----------
@router.message(lambda m: m.text and m.text.lower().startswith(("бан", ".бан", "!бан")))
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

    try:
        await message.bot.ban_chat_member(message.chat.id, target.id)
        await message.reply(f"🔨 {target.mention_html()} забанен.")
    except Exception as e:
        await message.reply(f"❌ Ошибка: {e}")


# ---------- РАЗБАН ----------
@router.message(lambda m: m.text and m.text.lower().startswith(("разбан", ".разбан", "!разбан", "унбан")))
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


# ---------- КИК ----------
@router.message(lambda m: m.text and m.text.lower().startswith(("кик", ".кик", "!кик")))
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


# ---------- ВАРН ----------
@router.message(lambda m: m.text and m.text.lower().startswith(("варн", ".варн", "!варн", "пред", ".пред")))
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


# ---------- СНЯТЬ ВАРН ----------
@router.message(lambda m: m.text and m.text.lower().startswith(("унварн", ".унварн", "!унварн", "снятьварн")))
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
