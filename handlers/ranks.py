from aiogram import Router, types
from aiogram.filters import Command
from database.crud import get_user, set_rank
from middlewares.check_rank import has_rank, get_user_rank

router = Router()

RANK_NAMES = {0: "Участник", 1: "Мл. модератор", 2: "Ст. модератор",
              3: "Мл. администратор", 4: "Ст. администратор", 5: "Создатель"}

@router.message(Command("rank"))
async def cmd_rank(message: types.Message):
    if not message.reply_to_message:
        rank = await get_user_rank(message.from_user.id, message.chat.id)
        await message.reply(f"Ваш ранг: <b>{RANK_NAMES.get(rank, 'Неизвестно')}</b>")
        return
    target = message.reply_to_message.from_user
    rank = await get_user_rank(target.id, message.chat.id)
    await message.reply(f"Ранг {target.mention_html()}: <b>{RANK_NAMES.get(rank, 'Неизвестно')}</b>")

@router.message(Command("promote"))
async def cmd_promote(message: types.Message):
    if not await has_rank(message, 3):
        return
    if not message.reply_to_message:
        return
    target = message.reply_to_message.from_user
    current = await get_user_rank(target.id, message.chat.id)
    if current >= 5:
        await message.reply("Максимальный ранг.")
        return
    set_rank(target.id, message.chat.id, current + 1)
    await message.reply(f"⬆️ {target.mention_html()} повышен до <b>{RANK_NAMES[current + 1]}</b>")

@router.message(Command("demote"))
async def cmd_demote(message: types.Message):
    if not await has_rank(message, 4):
        return
    if not message.reply_to_message:
        return
    target = message.reply_to_message.from_user
    current = await get_user_rank(target.id, message.chat.id)
    if current <= 0:
        return
    set_rank(target.id, message.chat.id, current - 1)
    await message.reply(f"⬇️ {target.mention_html()} понижен до <b>{RANK_NAMES[current - 1]}</b>")
