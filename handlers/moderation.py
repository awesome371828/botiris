from aiogram import Router, types
from aiogram.filters import Command
from aiogram.types import ChatPermissions
from datetime import datetime, timedelta
from database.crud import get_user, create_user, add_warn, reset_warns, get_chat
from middlewares.check_rank import has_rank

router = Router()


@router.message(Command("warn"))
async def cmd_warn(message: types.Message):
    if not await has_rank(message, 1):
        await message.reply("Недостаточно прав.")
        return
    if not message.reply_to_message:
        await message.reply("Ответьте на сообщение нарушителя.")
        return

    target = message.reply_to_message.from_user
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
            await message.answer(f"Ошибка: {e}")


@router.message(Command("mute"))
async def cmd_mute(message: types.Message):
    if not await has_rank(message, 1):
        await message.reply("Недостаточно прав.")
        return
    if not message.reply_to_message:
        await message.reply("Ответьте на сообщение нарушителя.")
        return

    target = message.reply_to_message.from_user
    try:
        await message.bot.restrict_chat_member(
            chat_id=message.chat.id,
            user_id=target.id,
            permissions=ChatPermissions(can_send_messages=False),
            until_date=datetime.now() + timedelta(minutes=15),
        )
        await message.reply(f"🔇 {target.mention_html()} замучен на 15 минут.")
    except Exception as e:
        await message.reply(f"Ошибка: {e}")


@router.message(Command("unmute"))
async def cmd_unmute(message: types.Message):
    if not await has_rank(message, 1):
        await message.reply("Недостаточно прав.")
        return
    if not message.reply_to_message:
        await message.reply("Ответьте на сообщение нарушителя.")
        return

    target = message.reply_to_message.from_user
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
        await message.reply(f"Ошибка: {e}")


@router.message(Command("ban"))
async def cmd_ban(message: types.Message):
    if not await has_rank(message, 2):
        await message.reply("Недостаточно прав.")
        return
    if not message.reply_to_message:
        await message.reply("Ответьте на сообщение нарушителя.")
        return

    target = message.reply_to_message.from_user
    try:
        await message.bot.ban_chat_member(message.chat.id, target.id)
        await message.reply(f"🔨 {target.mention_html()} забанен.")
    except Exception as e:
        await message.reply(f"Ошибка: {e}")
