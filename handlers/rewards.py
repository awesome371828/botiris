from aiogram import Router, types
from aiogram.filters import Command
from database.supabase_client import supabase

router = Router()

@router.message(Command("award"))
async def cmd_award(message: types.Message):
    if not message.reply_to_message:
        await message.reply("Ответьте на сообщение пользователя.")
        return
    args = message.text.split(maxsplit=2)
    if len(args) < 3:
        await message.reply("Использование: /award <степень 1-8> <описание>")
        return
    try:
        degree = int(args[1])
        if not 1 <= degree <= 8:
            raise ValueError
    except ValueError:
        await message.reply("Степень должна быть от 1 до 8.")
        return
    target = message.reply_to_message.from_user
    supabase.table("rewards").insert({
        "user_id": target.id, "chat_id": message.chat.id,
        "degree": degree, "description": args[2],
        "awarded_by": message.from_user.id
    }).execute()
    await message.reply(f"🏅 {target.mention_html()} награждён медалью степени {degree}.")
