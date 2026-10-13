from aiogram import Router, types
from aiogram.filters import Command
from datetime import datetime, timedelta
from database.crud import get_user, create_user, update_balance, log_transaction
from database.supabase_client import supabase

router = Router()


@router.message(Command("balance"))
async def cmd_balance(message: types.Message):
    user = get_user(message.from_user.id, message.chat.id)
    if not user:
        create_user(message.from_user.id, message.chat.id, message.from_user.username, message.from_user.first_name)
        user = get_user(message.from_user.id, message.chat.id)

    await message.answer(
        f"💰 Баланс:\n"
        f"Ириски: <b>{user['balance']}</b>\n"
        f"Ирис-коины: <b>{user['gold']}</b>"
    )


@router.message(Command("bonus"))
async def cmd_bonus(message: types.Message):
    user = get_user(message.from_user.id, message.chat.id)
    if not user:
        create_user(message.from_user.id, message.chat.id, message.from_user.username, message.from_user.first_name)
        user = get_user(message.from_user.id, message.chat.id)

    now = datetime.now()
    if user["last_bonus"]:
        last = datetime.fromisoformat(user["last_bonus"].replace("Z", "+00:00"))
        if now - last < timedelta(hours=24):
            await message.reply("⏳ Бонус уже получен. Приходите позже.")
            return

    amount = 25
    update_balance(message.from_user.id, message.chat.id, amount, "sweets")
    log_transaction(message.from_user.id, message.chat.id, amount, "sweets", "Ежедневный бонус")
    supabase.table("users").update({"last_bonus": now.isoformat()}) \
        .eq("user_id", message.from_user.id).eq("chat_id", message.chat.id).execute()
    await message.reply(f"🎁 Бонус: <b>{amount}</b> ирисок!")
