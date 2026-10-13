from aiogram import Router, types
from aiogram.filters import Command
from database.crud import get_user, create_user
from database.supabase_client import supabase

router = Router()

@router.message(Command("marry"))
async def cmd_marry(message: types.Message):
    if not message.reply_to_message:
        await message.reply("Ответьте на сообщение пользователя.")
        return
    target = message.reply_to_message.from_user
    if target.id == message.from_user.id:
        return
    user = get_user(message.from_user.id, message.chat.id) or create_user(
        message.from_user.id, message.chat.id, message.from_user.username, message.from_user.first_name)
    if user["married_to"]:
        await message.reply("Вы уже в браке.")
        return
    supabase.table("users").update({"married_to": target.id}) \
        .eq("user_id", message.from_user.id).eq("chat_id", message.chat.id).execute()
    supabase.table("users").update({"married_to": message.from_user.id}) \
        .eq("user_id", target.id).eq("chat_id", message.chat.id).execute()
    await message.reply(f"💍 {message.from_user.mention_html()} и {target.mention_html()} теперь в браке!")
