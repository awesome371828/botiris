from aiogram import types
from database.crud import get_user, set_rank

OWNER_ID = 6652898792  # Ваш ID владельца

async def get_user_rank(user_id, chat_id):
    user = get_user(user_id, chat_id)
    return user["rank"] if user else 0

async def has_rank(message: types.Message, required: int) -> bool:
    if message.from_user.id == OWNER_ID:
        return True
    member = await message.chat.get_member(message.from_user.id)
    if member.status == "creator":
        return True
    if member.status == "administrator":
        return required <= 5
    rank = await get_user_rank(message.from_user.id, message.chat.id)
    return rank >= required
